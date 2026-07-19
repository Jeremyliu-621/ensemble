// sketch.ino — UNO Q microcontroller side of the Phoneharmonic Wand (App Lab).
//
// Two jobs, no decisions:
//   UPLINK   sample the Modulino Movement IMU ~60 Hz, convert to the server's
//            units (accel m/s^2 WITH gravity, gyro RAW deg/s) and push each
//            frame to the Linux side over Bridge on topic "imu".
//   DOWNLINK receive the show state the laptop reflected back (playing / mode /
//            aim) on Bridge topic "cmd" and drive local feedback (LED / buzzer).
//
// The MCU never integrates the gyro, never classifies a gesture, never runs a
// state machine — the laptop server does all of that (see firmware/BACKEND_NOTES.md).
//
// MCU<->Linux Bridge contract (kept CSV so the MCU needs no JSON parser):
//   MCU  -> Linux  topic "imu":   "tw,ax,ay,az,gx,gy,gz"  (one frame per notify)
//   MCU  -> Linux  topic "range": "mm"   ToF distance ~10 Hz -> server wand.range
//   Linux -> MCU   topic "cmd":   "playing,mode,aim"      e.g. "1,det,s2"
//                                playing = 0|1, mode = ai|det, aim = section id or ""
//
// NOTE: includes + the Bridge/Modulino API match the TESTED stream_probe app
// (Arduino_RouterBridge + Arduino_Modulino, pinned in sketch/sketch.yaml).
// Documented fallback if Bridge fights us: CSV-over-Serial (same payloads).

#include <Arduino_Modulino.h>
#include <Arduino_RouterBridge.h>

ModulinoMovement imu;
ModulinoDistance dist;   // Modulino Distance (ToF) @ 0x29 on the same Qwiic chain

// ---- feedback pins (optional; safe no-ops if nothing is wired) ----
static const int PIN_LED_PLAY = LED_BUILTIN;  // solid = playing, blink = paused
static const int PIN_LED_MODE = 2;            // on = det mode, off = ai mode
static const int PIN_BUZZER   = 3;            // short blip on aim change

// ---- latest reflected show state ----
static bool  gPlaying = false;
static String gMode   = "ai";
static String gAim    = "";

static const float G = 9.81f;                 // g -> m/s^2 (keep gravity)
static const unsigned long SAMPLE_US = 16667; // ~60 Hz

// Parse "playing,mode,aim" pushed from Linux and apply local feedback.
// Bridge.provide wraps this in std::function<void(Args...)> and default-
// constructs a std::tuple<Args...> to decode into — a reference-typed Args
// (const String&) can't be default-constructed, so this must take by value.
void onCmd(String payload) {
  int c1 = payload.indexOf(',');
  int c2 = payload.indexOf(',', c1 + 1);
  if (c1 < 0 || c2 < 0) return;
  gPlaying = (payload.substring(0, c1).toInt() != 0);
  gMode    = payload.substring(c1 + 1, c2);
  String newAim = payload.substring(c2 + 1);
  bool aimChanged = (newAim != gAim);
  gAim = newAim;
  applyState(aimChanged);
}

void applyState(bool aimChanged) {
  digitalWrite(PIN_LED_MODE, gMode == "det" ? HIGH : LOW);
  digitalWrite(PIN_LED_PLAY, gPlaying ? HIGH : LOW);   // paused handled by blink in loop()
  if (aimChanged && gAim.length()) {
    digitalWrite(PIN_BUZZER, HIGH);
    delay(15);
    digitalWrite(PIN_BUZZER, LOW);
  }
}

void setup() {
  pinMode(PIN_LED_PLAY, OUTPUT);
  pinMode(PIN_LED_MODE, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);

  Modulino.begin();              // UNO Q auto-selects the Qwiic bus (matches the tested
                                 // stream_probe). If a sensor isn't found, the documented
                                 // alternative is Modulino.begin(Wire1) — see hardware-wand.md.
  imu.begin();                   // Modulino Movement @ 0x6A
  dist.begin();                  // Modulino Distance (ToF) @ 0x29

  Bridge.begin();
  Bridge.provide("cmd", onCmd);  // receive reflected show state from Linux
}

char line[96];

void loop() {
  static unsigned long lastSample = 0;
  static unsigned long lastBlink = 0;

  unsigned long nowUs = micros();
  if (nowUs - lastSample >= SAMPLE_US) {
    lastSample = nowUs;
    imu.update();
    unsigned long tw = millis();
    // accel in m/s^2 WITH gravity; gyro RAW deg/s — do NOT integrate here.
    snprintf(line, sizeof(line), "%lu,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f",
             tw,
             imu.getX() * G, imu.getY() * G, imu.getZ() * G,
             imu.getRoll(), imu.getPitch(), imu.getYaw());
    Bridge.notify("imu", line);  // Linux batches ~5 of these into one wand.imu
  }

  // Distance (ToF) at ~10 Hz -> Linux -> wand.range (proximity "squish").
  static unsigned long lastRange = 0;
  if (millis() - lastRange >= 100) {
    lastRange = millis();
    if (dist.available()) {
      float mm = dist.get();
      if (mm == mm) {              // reject NaN (invalid ToF reading)
        char rline[24];
        snprintf(rline, sizeof(rline), "%.0f", mm);
        Bridge.notify("range", rline);
      }
    }
  }

  // Paused = blink the play LED at ~2 Hz so the DJ can see the show is stopped.
  if (!gPlaying && millis() - lastBlink >= 250) {
    lastBlink = millis();
    digitalWrite(PIN_LED_PLAY, !digitalRead(PIN_LED_PLAY));
  }
}
