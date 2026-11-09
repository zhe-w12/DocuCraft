function scanOutline(items, threshold, readPosition) {
  let current = items[0];
  for (let index = 0; index < items.length; index += 1) {
    const position = readPosition(index, items[index]);
    if (!Number.isFinite(position)) continue;
    if (position <= threshold) current = items[index];
    else break;
  }
  return current;
}

export function findLastItemAtOrBefore(items, threshold, getPosition) {
  if (!Array.isArray(items) || !items.length || typeof getPosition !== "function") return null;

  const positions = new Map();
  const readPosition = (index, item) => {
    if (!positions.has(index)) positions.set(index, getPosition(item));
    return positions.get(index);
  };
  let low = 0;
  let high = items.length - 1;
  let currentIndex = 0;

  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const position = readPosition(middle, items[middle]);
    if (!Number.isFinite(position)) return scanOutline(items, threshold, readPosition);
    if (position <= threshold) {
      currentIndex = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }

  return items[currentIndex];
}
