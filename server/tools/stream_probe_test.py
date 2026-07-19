"""Laptop-side tests for the isolated UNO Q IMU stream probe.

No board, network, App Lab runtime, or running Phoneharmonic server is needed.

Run from the repository root:
    python server/tools/stream_probe_test.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from websockets.asyncio.server import serve

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
STREAMER_PATH = REPO / "firmware" / "uno_q" / "stream_probe" / "python" / "main.py"
MONITOR_PATH = SERVER / "tools" / "wand_monitor.py"
LAUNCHER = REPO / "firmware" / "uno_q" / "stream_probe" / "run_probe.sh"

sys.path.insert(0, str(SERVER))

from imu_telemetry import ImuTelemetry  # noqa: E402
from hub import ClientConn  # noqa: E402
from main import App  # noqa: E402
from network_address import address_score, format_url_host, websocket_url  # noqa: E402


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


streamer = _load("phoneharmonic_probe_streamer", STREAMER_PATH)
monitor = _load("phoneharmonic_wand_monitor", MONITOR_PATH)


class StreamerTests(unittest.TestCase):
    def test_csv_parser_accepts_exact_finite_row(self) -> None:
        row = streamer.parse_imu_csv("12,0.1,-0.2,9.81,1,2,-3")
        self.assertEqual(row, [12.0, 0.1, -0.2, 9.81, 1.0, 2.0, -3.0])

    def test_csv_parser_rejects_malformed_rows(self) -> None:
        for payload in (
            "1,2,3", "1,2,3,4,5,6,nope", "1,2,3,4,5,6,nan",
            "1,2,3,4,5,6,inf", b"\xff", None, [1, 2, 3, 4, 5, 6, 7],
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(streamer.parse_imu_csv(payload))

    def test_queue_is_bounded_and_discards_oldest(self) -> None:
        samples = streamer.SampleBuffer(maxsize=2)
        for tw in (1.0, 2.0, 3.0):
            samples.put([tw, 0.0, 0.0, 9.81, 0.0, 0.0, 0.0])
        self.assertEqual(samples.snapshot(), {
            "accepted": 3, "rejected": 0, "dropped": 1, "queued": 2,
        })
        batch = samples.take_batch(2)
        self.assertEqual([row[0] for row in batch], [2.0, 3.0])

    def test_five_rows_batch_and_sequence_survives_reconnect_boundary(self) -> None:
        samples = streamer.SampleBuffer()
        client = streamer.StreamClient(
            streamer.ProbeConfig("ws://192.168.1.42:8080/ws", "lol1"), samples,
        )
        for tw in range(5):
            samples.put([float(tw), 0.0, 0.0, 9.81, 0.0, 0.0, 0.0])
        first = client.next_message()
        self.assertEqual(first["t"], "wand.imu")
        self.assertEqual(first["seq"], 1)
        self.assertEqual(len(first["frames"]), 5)

        # A reconnect uses the same StreamClient instance; it must not reset seq.
        client.client_id = "same-board"
        for tw in range(5, 10):
            samples.put([float(tw), 0.0, 0.0, 9.81, 0.0, 0.0, 0.0])
        second = client.next_message()
        self.assertEqual(second["seq"], 2)
        self.assertEqual(client.client_id, "same-board")


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.telemetry = ImuTelemetry()
        self.valid = [100, 0, 0, 9.81, 0, 0, 25]

    def test_validates_counts_and_returns_only_valid_frames(self) -> None:
        forwarded = self.telemetry.ingest(5, [self.valid, [1, 2]], 1234.0)
        self.assertEqual(len(forwarded), 1)
        self.assertTrue(all(isinstance(value, float) for value in forwarded[0]))
        snapshot = self.telemetry.snapshot()
        self.assertEqual(snapshot["batches"], 1)
        self.assertEqual(snapshot["frames"], 1)
        self.assertEqual(snapshot["invalid_frames"], 1)
        self.assertEqual(snapshot["seq"], 5)
        self.assertEqual(snapshot["last_rx_server_ms"], 1234.0)

    def test_forward_sequence_gaps_and_initial_baseline(self) -> None:
        self.telemetry.ingest(100, [self.valid], 1.0)
        self.assertEqual(self.telemetry.snapshot()["seq_gaps"], 0)
        self.telemetry.ingest(103, [self.valid], 2.0)
        self.assertEqual(self.telemetry.snapshot()["seq_gaps"], 2)

    def test_invalid_sequence_rejects_entire_batch(self) -> None:
        self.assertEqual(self.telemetry.ingest(-1, [self.valid], 1.0), [])
        self.assertEqual(self.telemetry.snapshot()["invalid_frames"], 1)

    def test_reset_clears_new_wand_diagnostics(self) -> None:
        self.telemetry.ingest(1, [self.valid], 1.0)
        self.telemetry.reset()
        self.assertEqual(self.telemetry.snapshot(), {
            "seq": None,
            "batches": 0,
            "frames": 0,
            "invalid_frames": 0,
            "seq_gaps": 0,
            "last_frame": None,
            "last_rx_server_ms": None,
        })


def make_snapshots(
    *,
    sample_rate: float = 60.0,
    batch_rate: float = 12.0,
    gravity: float = 9.81,
    moving: bool = True,
    invalid_frames: int = 0,
    seq_gaps: int = 0,
) -> list:
    snapshots = []
    step = 0.15
    for index in range(int(30.0 / step) + 1):
        elapsed = index * step
        in_motion = 8.0 <= elapsed <= 20.0
        gz = 30.0 if in_motion and moving else 0.0
        yaw = (elapsed - 8.0) * 10.0 if in_motion and moving else (120.0 if elapsed > 20 and moving else 0.0)
        imu = {
            "seq": round(elapsed * batch_rate),
            "batches": round(elapsed * batch_rate),
            "frames": round(elapsed * sample_rate),
            "invalid_frames": invalid_frames,
            "seq_gaps": seq_gaps,
            "last_frame": [elapsed * 1000.0, 0.0, 0.0, gravity, 0.0, 0.0, gz],
            "last_rx_server_ms": elapsed * 1000.0,
        }
        snapshots.append(monitor.ProbeSnapshot(elapsed, elapsed, yaw, imu))
    return snapshots


class MonitorTests(unittest.TestCase):
    def assert_check(self, checks, name: str, expected: bool) -> None:
        check = next(item for item in checks if item.name == name)
        self.assertEqual(check.passed, expected, check)

    def test_nominal_sixty_hz_fixture_passes(self) -> None:
        checks = monitor.evaluate_probe(make_snapshots(), 30.0)
        self.assertTrue(all(check.passed for check in checks), checks)

    def test_failure_fixtures_identify_independent_boundaries(self) -> None:
        fixtures = (
            (make_snapshots(gravity=1.0), "gravity units"),
            (make_snapshots(moving=False), "physical yaw movement"),
            (make_snapshots(invalid_frames=1), "valid frames"),
            (make_snapshots(seq_gaps=2), "sequence continuity"),
            (make_snapshots(sample_rate=20.0, batch_rate=4.0), "sample rate"),
        )
        for snapshots, failed_name in fixtures:
            with self.subTest(failed_name=failed_name):
                self.assert_check(monitor.evaluate_probe(snapshots, 30.0), failed_name, False)

    def test_missing_hardware_fails_before_stream_metrics(self) -> None:
        checks = monitor.evaluate_probe([], 30.0, hardware_connected=False)
        self.assertFalse(all(check.passed for check in checks))
        self.assert_check(checks, "hardware wand", False)

    def test_stream_stopping_early_fails_receive_continuity(self) -> None:
        snapshots = [item for item in make_snapshots() if item.elapsed <= 10.0]
        checks = monitor.evaluate_probe(snapshots, 30.0)
        self.assert_check(checks, "receive continuity", False)

    def test_ipv6_loopback_endpoint_handshake(self) -> None:
        async def exercise() -> None:
            async def handler(ws) -> None:
                hello = json.loads(await ws.recv())
                await ws.send(json.dumps({
                    "t": "welcome",
                    "v": 1,
                    "role": hello.get("role"),
                    "client_id": "ipv6-test-client",
                }))

            try:
                server = await serve(handler, "::1", 0)
            except OSError as exc:
                self.skipTest(f"IPv6 loopback unavailable: {exc}")
            try:
                port = server.sockets[0].getsockname()[1]
                url = websocket_url("::1", port)
                self.assertEqual(url, f"ws://[::1]:{port}/ws")
                self.assertEqual(await monitor.check_server(url, "lol1"), 0)
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(exercise())


class NetworkAddressTests(unittest.TestCase):
    def test_url_hosts_bracket_ipv6_only(self) -> None:
        self.assertEqual(format_url_host("192.168.1.42"), "192.168.1.42")
        self.assertEqual(format_url_host("2605:8d80:440:7d4c::10"),
                         "[2605:8d80:440:7d4c::10]")

    def test_ipv6_override_is_not_scored_as_virtual(self) -> None:
        self.assertGreater(address_score("2605:8d80:440:7d4c::10"), 0)
        self.assertGreater(address_score("fd12:3456:789a::10"), 0)

    def test_server_welcome_uses_bracketed_ipv6_urls(self) -> None:
        class RecordingSocket:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def send(self, payload: str) -> None:
                self.messages.append(payload)

            async def close(self) -> None:
                pass

        async def exercise() -> None:
            with mock.patch.dict(os.environ, {"WM_LAN_IP": "2605:8d80:440:7d4c::10"}):
                app = App()
            ws = RecordingSocket()
            await app._on_hello(ws, {
                "t": "hello", "v": 1, "role": "admin", "client_id": "ipv6-admin",
            }, "admin")
            welcome = json.loads(ws.messages[0])
            config = welcome["config"]
            base = "http://[2605:8d80:440:7d4c::10]:8080"
            self.assertEqual(config["wand_url"], f"{base}/section/?s=lol1")
            self.assertEqual(config["join_url"], f"{base}/section/?s=lol1")
            self.assertEqual(config["cv_url"], f"{base}/cvwand/")
            self.assertEqual(config["lan_ip"], "2605:8d80:440:7d4c::10")

        asyncio.run(exercise())


class CvStateTests(unittest.TestCase):
    class RecordingSocket:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, payload: str) -> None:
            self.messages.append(payload)

    def test_valid_cv_state_is_logged_once_per_gesture_or_mode_change(self) -> None:
        async def exercise() -> None:
            app = App()
            ws = self.RecordingSocket()
            conn = ClientConn("cv-client", "admin", ws, name="cv-gestures")
            msg = {"t": "cv.state", "gesture": "PALM", "mode": "SELECT", "confidence": 0.91}

            with self.assertLogs("main", level="INFO") as captured:
                await app._dispatch(conn, msg)
                await app._dispatch(conn, {**msg, "confidence": 0.95})

            cv_lines = [line for line in captured.output if "cv state" in line]
            self.assertEqual(len(cv_lines), 1)
            self.assertIn("gesture=PALM mode=SELECT confidence=91%", cv_lines[0])
            self.assertEqual(conn.extra["cv_state"]["confidence"], 0.95)

        asyncio.run(exercise())

    def test_invalid_or_non_admin_cv_state_is_rejected(self) -> None:
        async def exercise() -> None:
            app = App()
            bad_ws = self.RecordingSocket()
            admin = ClientConn("bad-cv", "admin", bad_ws)
            await app._dispatch(admin, {
                "t": "cv.state", "gesture": "LOG\nINJECTION", "mode": "AI", "confidence": 1,
            })
            self.assertEqual(json.loads(bad_ws.messages[-1])["code"], "bad_cv_state")

            section_ws = self.RecordingSocket()
            section = ClientConn("section-client", "section", section_ws)
            await app._dispatch(section, {
                "t": "cv.state", "gesture": "FIST", "mode": "AI", "confidence": 1,
            })
            self.assertEqual(json.loads(section_ws.messages[-1])["code"], "forbidden")

        asyncio.run(exercise())


class LauncherTests(unittest.TestCase):
    def test_shell_syntax(self) -> None:
        subprocess.run(["bash", "-n", str(LAUNCHER)], check=True)

    def test_monitor_cli_runs_directly_from_repo_root(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MONITOR_PATH), "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--startup-timeout", result.stdout)

    def test_preflight_checks_code_and_dependencies_without_board(self) -> None:
        result = subprocess.run(
            [str(LAUNCHER), "--board", "arduino@uno-q.local",
             "--server-ip", "192.168.1.42", "--preflight-only"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local preflight PASS", result.stdout)

    def test_dry_run_has_no_network_dependency(self) -> None:
        result = subprocess.run(
            [str(LAUNCHER), "--board", "arduino@uno-q.local",
             "--server-ip", "192.168.1.42", "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("no local or remote state changed", result.stdout)

    def test_dry_run_formats_ipv4_and_ipv6_urls(self) -> None:
        fixtures = (
            ("192.168.1.42", "ws://192.168.1.42:8080/ws"),
            ("2605:8d80:0440:7d4c:0000:0000:0000:0010",
             "ws://[2605:8d80:440:7d4c::10]:8080/ws"),
        )
        for address, expected_url in fixtures:
            with self.subTest(address=address):
                result = subprocess.run(
                    [str(LAUNCHER), "--board", "arduino@uno-q.local",
                     "--server-ip", address, "--dry-run"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertIn(f"server:     {expected_url}", result.stdout)

    def test_dry_run_rejects_loopback(self) -> None:
        for address in ("127.0.0.1", "::1"):
            with self.subTest(address=address):
                result = subprocess.run(
                    [str(LAUNCHER), "--board", "arduino@uno-q.local",
                     "--server-ip", address, "--dry-run"],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot be loopback", result.stderr)

    def test_dry_run_rejects_unreachable_address_classes(self) -> None:
        fixtures = (
            ("0.0.0.0", "cannot be unspecified"),
            ("::", "cannot be unspecified"),
            ("224.0.0.1", "cannot be multicast"),
            ("ff02::1", "cannot be multicast"),
            ("169.254.10.20", "unscoped link-local"),
            ("fe80::1234", "unscoped link-local"),
            ("fe80::1234%en0", "without an interface scope"),
            ("not-an-ip", "bare numeric IPv4 or IPv6"),
            ("[2605:8d80:440:7d4c::10]", "bare numeric IPv4 or IPv6"),
        )
        for address, expected in fixtures:
            with self.subTest(address=address):
                result = subprocess.run(
                    [str(LAUNCHER), "--board", "arduino@uno-q.local",
                     "--server-ip", address, "--dry-run"],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_dry_run_rejects_zero_duration_and_invalid_board(self) -> None:
        for extra_args, expected in (
            (["--duration", "0.0"], "positive number"),
            (["--board", "-oProxyCommand=bad"], "USER@HOST"),
        ):
            with self.subTest(extra_args=extra_args):
                args = [str(LAUNCHER), "--board", "arduino@uno-q.local",
                        "--server-ip", "192.168.1.42", "--dry-run"]
                if extra_args[0] == "--board":
                    args[1:3] = extra_args
                else:
                    args.extend(extra_args)
                result = subprocess.run(args, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    unittest.main(verbosity=2)
