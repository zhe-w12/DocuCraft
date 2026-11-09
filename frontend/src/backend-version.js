export function isBackendVersionCompatible(
  health,
  expectedApiCompatibilityVersion,
  expectedFingerprint = expectedApiCompatibilityVersion,
) {
  if (health && Object.prototype.hasOwnProperty.call(health, "api_compatibility_version")) {
    return Number.isSafeInteger(expectedApiCompatibilityVersion)
      && Number.isSafeInteger(health.api_compatibility_version)
      && health.api_compatibility_version === expectedApiCompatibilityVersion;
  }

  // Older backends do not report the API contract version; keep their strict
  // source fingerprint check until the paired compatibility-aware release lands.
  const actualFingerprint = health?.source_fingerprint;
  return typeof expectedFingerprint === "string"
    && /^[a-f\d]{16}$/i.test(expectedFingerprint)
    && typeof actualFingerprint === "string"
    && /^[a-f\d]{16}$/i.test(actualFingerprint)
    && actualFingerprint.toLowerCase() === expectedFingerprint.toLowerCase();
}
