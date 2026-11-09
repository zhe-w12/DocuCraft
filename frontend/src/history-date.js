import { parseHistoryTimestamp } from "./history-order.js";

export function createHistoryDateFormatter(createFormatter = () => new Intl.DateTimeFormat("zh-CN", {
  month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
})) {
  let formatter = null;

  return timestamp => {
    try {
      const parsedTimestamp = parseHistoryTimestamp(timestamp);
      if (parsedTimestamp === null) return "时间未知";
      formatter ||= createFormatter();
      return formatter.format(new Date(parsedTimestamp));
    } catch {
      return "时间未知";
    }
  };
}
