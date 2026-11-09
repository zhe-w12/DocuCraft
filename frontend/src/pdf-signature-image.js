import { clamp } from "./pdf-editor-geometry.js";

const MIN_LIGHT_BACKGROUND_BRIGHTNESS = 128;
const MAX_LIGHT_BACKGROUND_COLOR_SPREAD = 96;

export function removeLightSignatureBackground(imageData, {
  backgroundDeadZone = 8,
  featherDistance = 56,
  maxColorSpread = 34,
} = {}) {
  const pixels = imageData?.data;
  if (!pixels) return imageData;

  const { width, height } = imageData;
  let background = [255, 255, 255];
  if (width > 0 && height > 0 && pixels.length >= width * height * 4) {
    const border = [[], [], []];
    const addBorderPixel = (x, y) => {
      const offset = (y * width + x) * 4;
      if (pixels[offset + 3] < 32) return;
      const red = pixels[offset];
      const green = pixels[offset + 1];
      const blue = pixels[offset + 2];
      const brightness = (red + green + blue) / 3;
      const spread = Math.max(red, green, blue) - Math.min(red, green, blue);
      if (
        brightness < MIN_LIGHT_BACKGROUND_BRIGHTNESS
        || spread > MAX_LIGHT_BACKGROUND_COLOR_SPREAD
      ) return;
      border[0].push(red);
      border[1].push(green);
      border[2].push(blue);
    };
    const edge = 2 * width + 2 * Math.max(0, height - 2);
    const step = Math.max(1, Math.ceil(edge / 4096));
    for (let x = 0; x < width; x += step) {
      addBorderPixel(x, 0);
      if (height > 1) addBorderPixel(x, height - 1);
    }
    for (let y = step; y < height - 1; y += step) {
      addBorderPixel(0, y);
      if (width > 1) addBorderPixel(width - 1, y);
    }
    if (border[0].length) {
      const median = (values) => {
        values.sort((left, right) => left - right);
        return values[Math.floor(values.length / 2)];
      };
      background = border.map(median);
    }
  }

  const backgroundBrightness = (background[0] + background[1] + background[2]) / 3;
  const backgroundSpread = Math.max(...background) - Math.min(...background);
  if (
    backgroundBrightness < MIN_LIGHT_BACKGROUND_BRIGHTNESS
    || backgroundSpread > MAX_LIGHT_BACKGROUND_COLOR_SPREAD
  ) {
    background = [255, 255, 255];
  }

  // A colored pen can differ from light paper much more in one color channel
  // than another. Estimate the strongest ink contrast per channel so its
  // antialiased pixels can be unmatted instead of keeping the paper halo.
  const foregroundDelta = [0, 0, 0];
  for (let offset = 0; offset < pixels.length; offset += 4) {
    if (pixels[offset + 3] < 32) continue;
    for (let channel = 0; channel < 3; channel += 1) {
      foregroundDelta[channel] = Math.max(
        foregroundDelta[channel],
        background[channel] - pixels[offset + channel],
      );
    }
  }
  const referenceContrast = Math.max(...foregroundDelta);
  const foregroundColorSpread = Math.max(...foregroundDelta) - Math.min(...foregroundDelta);
  const useColorAwareCoverage = referenceContrast >= featherDistance
    || (
      referenceContrast > backgroundDeadZone
      && foregroundColorSpread > maxColorSpread
    );

  for (let offset = 0; offset < pixels.length; offset += 4) {
    const alpha = pixels[offset + 3] / 255;
    if (!alpha) continue;

    const red = pixels[offset];
    const green = pixels[offset + 1];
    const blue = pixels[offset + 2];
    const redDelta = background[0] - red;
    const greenDelta = background[1] - green;
    const blueDelta = background[2] - blue;
    const darkness = (redDelta + greenDelta + blueDelta) / 3;
    const colorSpread = Math.max(redDelta, greenDelta, blueDelta) - Math.min(redDelta, greenDelta, blueDelta);
    let foregroundCoverage;
    if (useColorAwareCoverage) {
      const normalizedContrast = Math.max(
        foregroundDelta[0] > 0 ? Math.max(redDelta, 0) / foregroundDelta[0] : 0,
        foregroundDelta[1] > 0 ? Math.max(greenDelta, 0) / foregroundDelta[1] : 0,
        foregroundDelta[2] > 0 ? Math.max(blueDelta, 0) / foregroundDelta[2] : 0,
      );
      if (normalizedContrast <= backgroundDeadZone / referenceContrast) {
        pixels[offset] = 0;
        pixels[offset + 1] = 0;
        pixels[offset + 2] = 0;
        pixels[offset + 3] = 0;
        continue;
      }
      foregroundCoverage = clamp(normalizedContrast, 0, 1);
    } else {
      if (colorSpread > maxColorSpread || darkness <= backgroundDeadZone) {
        if (darkness <= backgroundDeadZone && colorSpread <= maxColorSpread) {
          pixels[offset] = 0;
          pixels[offset + 1] = 0;
          pixels[offset + 2] = 0;
          pixels[offset + 3] = 0;
        }
        continue;
      }

      foregroundCoverage = clamp(
        (darkness - backgroundDeadZone) / (featherDistance - backgroundDeadZone),
        0,
        1,
      );
    }
    const outputAlpha = alpha * foregroundCoverage;
    if (outputAlpha <= 0.01) {
      pixels[offset] = 0;
      pixels[offset + 1] = 0;
      pixels[offset + 2] = 0;
      pixels[offset + 3] = 0;
      continue;
    }

    const unmatte = (channel, backgroundChannel) => clamp(
      (channel - backgroundChannel * (1 - foregroundCoverage)) / foregroundCoverage,
      0,
      255,
    );
    pixels[offset] = unmatte(red, background[0]);
    pixels[offset + 1] = unmatte(green, background[1]);
    pixels[offset + 2] = unmatte(blue, background[2]);
    pixels[offset + 3] = Math.round(outputAlpha * 255);
  }
  return imageData;
}

export function drawSignatureImage(context, image, signature, canvasWidth, canvasHeight) {
  const width = signature.width * canvasWidth;
  const height = signature.height * canvasHeight;
  const centerX = (signature.x + signature.width / 2) * canvasWidth;
  const centerY = (signature.y + signature.height / 2) * canvasHeight;
  context.save();
  context.globalAlpha = signature.opacity ?? 1;
  context.translate(centerX, centerY);
  context.rotate((signature.rotation || 0) * Math.PI / 180);
  context.drawImage(image, -width / 2, -height / 2, width, height);
  context.restore();
}
