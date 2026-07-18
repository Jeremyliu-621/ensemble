// Phoneharmonic isolated IMU stream probe — UNO Q MCU side.
//
// Sample the Modulino Movement at ~60 Hz and notify the UNO Q Linux side with
// one CSV row: tw,ax,ay,az,gx,gy,gz. Networking and batching live on Linux.

#include <Arduino_Modulino.h>
#include <Arduino_RouterBridge.h>
#include <math.h>

ModulinoMovement movement;

static constexpr unsigned long SAMPLE_INTERVAL_US = 16667UL;
static constexpr float GRAVITY_MS2 = 9.81f;

unsigned long nextSampleUs = 0;
unsigned long sampleCount = 0;
unsigned long lastHealthMs = 0;
unsigned long lastSensorErrorMs = 0;
char sampleCsv[112];

static void haltWithMessage(const char* message) {
  Monitor.println(message);
  while (true) {
    delay(1000);
  }
}

void setup() {
  const bool bridgeReady = Bridge.begin();
  const bool monitorReady = Monitor.begin();
  Modulino.begin();
  const bool movementReady = movement.begin();

  if (!monitorReady) {
    // There is no usable Monitor transport in this case, but keep the failure
    // deterministic instead of attempting to stream from a partial startup.
    while (true) {
      delay(1000);
    }
  }
  if (!bridgeReady) {
    haltWithMessage("[imu-probe] FATAL: RouterBridge failed to start");
  }
  if (!movementReady) {
    haltWithMessage("[imu-probe] FATAL: Modulino Movement not detected");
  }

  nextSampleUs = micros();
  Monitor.println("[imu-probe] sensor and Bridge ready");
}

void loop() {
  const unsigned long nowUs = micros();
  if (static_cast<long>(nowUs - nextSampleUs) < 0) {
    return;
  }

  // Advance from the intended deadline instead of from the completion time, so
  // routine processing jitter does not slowly reduce the sample rate.
  nextSampleUs += SAMPLE_INTERVAL_US;
  if (static_cast<long>(nowUs - nextSampleUs) >= 0) {
    // If execution was delayed by more than one interval, resume from now. Do
    // not emit a burst of stale samples trying to catch up.
    nextSampleUs = nowUs + SAMPLE_INTERVAL_US;
  }

  const unsigned long tw = millis();
  if (movement.update() == 0) {
    if (tw - lastSensorErrorMs >= 1000UL) {
      lastSensorErrorMs = tw;
      Monitor.println("[imu-probe] waiting for a fresh Movement sample");
    }
    return;
  }

  const float ax = movement.getX() * GRAVITY_MS2;
  const float ay = movement.getY() * GRAVITY_MS2;
  const float az = movement.getZ() * GRAVITY_MS2;
  const float gx = movement.getRoll();
  const float gy = movement.getPitch();
  const float gz = movement.getYaw();

  if (!isfinite(ax) || !isfinite(ay) || !isfinite(az) ||
      !isfinite(gx) || !isfinite(gy) || !isfinite(gz)) {
    if (tw - lastSensorErrorMs >= 1000UL) {
      lastSensorErrorMs = tw;
      Monitor.println("[imu-probe] rejected non-finite Movement sample");
    }
    return;
  }

  snprintf(sampleCsv, sizeof(sampleCsv),
           "%lu,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f",
           tw, ax, ay, az, gx, gy, gz);
  Bridge.notify("imu_sample", sampleCsv);
  sampleCount++;

  if (tw - lastHealthMs >= 1000UL) {
    lastHealthMs = tw;
    Monitor.print("[imu-probe] samples=");
    Monitor.println(sampleCount);
  }
}
