#pragma once

#include <Arduino.h>

struct EpaperDashboardState {
  bool live;
  bool usageAvailable;
  uint8_t primaryUsed;
  uint8_t secondaryUsed;
  uint8_t personaState;
  uint32_t primaryResetsAt;
  uint32_t secondaryResetsAt;
  uint32_t utcNow;
  uint32_t tokens;
  const char* stateLabel;
};

// StickS3-only external 4.2-inch Waveshare dashboard. Other build targets use
// no-op implementations so their existing firmware remains unchanged.
void epaperDashboardBegin();
void epaperDashboardPoll(const EpaperDashboardState& state, bool refreshAllowed);
