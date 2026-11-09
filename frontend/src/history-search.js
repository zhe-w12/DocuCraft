export function createHistoryTitleMatcher(normalizeTitle = title => title.toLocaleLowerCase()) {
  const normalizedTitles = new WeakMap();

  return (item, query) => {
    if (!query) return true;
    const sourceTitle = String(item?.title || "");
    const cached = item && typeof item === "object" ? normalizedTitles.get(item) : null;
    let normalizedTitle = cached?.sourceTitle === sourceTitle ? cached.normalizedTitle : null;

    if (normalizedTitle === null) {
      normalizedTitle = normalizeTitle(sourceTitle);
      if (item && typeof item === "object") normalizedTitles.set(item, { sourceTitle, normalizedTitle });
    }

    return normalizedTitle.includes(query);
  };
}
