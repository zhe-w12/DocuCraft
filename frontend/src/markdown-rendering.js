function normalizeBracketMath(lines) {
  let codeFence = "";

  for (let index = 0; index < lines.length; index += 1) {
    const trimmed = lines[index].trim();
    if (codeFence) {
      if (trimmed.startsWith(codeFence)) codeFence = "";
      continue;
    }

    const fence = trimmed.match(/^(`{3,}|~{3,})/);
    if (fence) {
      codeFence = fence[1];
      continue;
    }

    const inlineStart = lines[index].indexOf("\\[");
    const inlineEnd = inlineStart < 0 ? -1 : lines[index].indexOf("\\]", inlineStart + 2);
    if (inlineEnd > inlineStart) {
      lines[index] = `${lines[index].slice(0, inlineStart)}$$${lines[index].slice(inlineStart + 2, inlineEnd)}$$${lines[index].slice(inlineEnd + 2)}`;
      continue;
    }

    if (trimmed !== "\\[") continue;
    let end = index + 1;
    let foundClosingBracket = false;
    let stoppedAtFence = false;
    for (; end < lines.length; end += 1) {
      const candidate = lines[end].trim();
      if (/^(`{3,}|~{3,})/.test(candidate)) {
        stoppedAtFence = true;
        break;
      }
      if (candidate !== "\\]") continue;
      foundClosingBracket = true;
      break;
    }

    if (foundClosingBracket) {
      lines[index] = lines[index].replace("\\[", () => "$$");
      lines[end] = lines[end].replace("\\]", () => "$$");
      index = end;
    } else if (stoppedAtFence) {
      // Let the next iteration enter the fenced block and skip its contents.
      index = end - 1;
    } else {
      break;
    }
  }

  return lines;
}

function normalizePlainBracketMath(lines) {
  let codeFence = "";
  const mathCommand = /\\[A-Za-z]+/;

  for (let index = 0; index < lines.length; index += 1) {
    const trimmed = lines[index].trim();
    if (codeFence) {
      if (trimmed.startsWith(codeFence)) codeFence = "";
      continue;
    }

    const fence = trimmed.match(/^(`{3,}|~{3,})/);
    if (fence) {
      codeFence = fence[1];
      continue;
    }

    if (trimmed !== "[") continue;
    let end = index + 1;
    let foundClosingBracket = false;
    let stoppedAtFence = false;
    for (; end < lines.length; end += 1) {
      const candidate = lines[end].trim();
      if (/^(`{3,}|~{3,})/.test(candidate)) {
        stoppedAtFence = true;
        break;
      }
      if (candidate !== "]") continue;
      foundClosingBracket = true;
      break;
    }

    if (foundClosingBracket) {
      const body = lines.slice(index + 1, end).join("\n").trim();
      if (mathCommand.test(body)) {
        lines[index] = lines[index].replace("[", () => "$$");
        lines[end] = lines[end].replace("]", () => "$$");
      }
      // Later openers before this closer have a subset of the same body.
      // Skip the span to avoid rescanning malformed or ordinary bracket text.
      index = end;
    } else if (stoppedAtFence) {
      // Let the next iteration enter the fenced block and skip its contents.
      index = end - 1;
    } else {
      break;
    }
  }

  return lines;
}

function normalizeMultilineDollarMath(lines) {
  const output = [];
  let codeFence = "";

  for (let index = 0; index < lines.length; index += 1) {
    const trimmed = lines[index].trim();
    if (codeFence) {
      output.push(lines[index]);
      if (trimmed.startsWith(codeFence)) codeFence = "";
      continue;
    }

    const fence = trimmed.match(/^(`{3,}|~{3,})/);
    if (fence) {
      codeFence = fence[1];
      output.push(lines[index]);
      continue;
    }

    if (trimmed !== "$$") {
      output.push(lines[index]);
      continue;
    }

    let close = index + 1;
    let nestedFence = "";
    for (; close < lines.length; close += 1) {
      const candidate = lines[close].trim();
      const candidateFence = candidate.match(/^(`{3,}|~{3,})/);
      if (nestedFence) {
        if (candidate.startsWith(nestedFence)) nestedFence = "";
        continue;
      }
      if (candidateFence) {
        nestedFence = candidateFence[1];
        continue;
      }
      if (candidate === "$$") break;
    }

    // Keep incomplete delimiters as-is while a response is streaming or if the source is malformed.
    if (close >= lines.length || nestedFence) {
      output.push(lines[index]);
      continue;
    }

    const body = lines.slice(index + 1, close).join("\n").trim();
    const equations = body
      .split(/\n\s*\n+/)
      .map(equation => equation
        .split("\n")
        .map(line => line.trim().replace(/^\*\*(.*)\*\*$/, "$1").replace(/^__(.*)__$/, "$1"))
        .filter(Boolean)
        .join(" "))
      .filter(Boolean);

    if (equations.length) {
      const indent = lines[index].match(/^\s*/)?.[0] || "";
      output.push(...equations.map(equation => `${indent}$$${equation}$$`));
    } else {
      output.push(lines[index], lines[close]);
    }
    index = close;
  }

  return output;
}

export function normalizeDisplayMathDelimiters(content) {
  const lines = normalizePlainBracketMath(normalizeBracketMath(String(content || "").split("\n")));
  return normalizeMultilineDollarMath(lines).join("\n");
}

function containsPairedDollarMath(content) {
  if (/\$\$[^$]*\S[^$]*\$\$/.test(content)) return true;

  let lineStart = 0;
  let firstOpen = -1;
  for (let index = 0; index < content.length; index += 1) {
    const character = content[index];
    if (character === "\n" || character === "\r" || character === "\u2028" || character === "\u2029") {
      lineStart = index + 1;
      firstOpen = -1;
      continue;
    }
    if (character !== "$") continue;

    const previous = index === lineStart ? "" : content[index - 1];
    const next = content[index + 1] || "";
    const previousIsBodyCharacter = previous !== "\\" && previous !== "$" && !/\s/.test(previous);
    if (firstOpen >= lineStart
      && index - firstOpen >= 2
      && previousIsBodyCharacter
      && !/[0-9]/.test(next)) return true;

    const hasValidPrefix = index === lineStart || (previous !== "\\" && !/[0-9]/.test(previous));
    if (firstOpen < lineStart && hasValidPrefix && next && !/\s/.test(next)) firstOpen = index;
  }
  return false;
}

function maskInlineCodeForMathDetection(content) {
  const runs = [];
  for (let index = 0; index < content.length;) {
    if (content[index] !== "`") {
      index += 1;
      continue;
    }
    const start = index;
    while (content[index] === "`") index += 1;
    runs.push({ start, end: index, length: index - start });
  }
  if (!runs.length) return content;

  const nextSameRun = new Array(runs.length);
  const latestRunByLength = new Map();
  for (let index = runs.length - 1; index >= 0; index -= 1) {
    const run = runs[index];
    nextSameRun[index] = latestRunByLength.get(run.length) ?? -1;
    latestRunByLength.set(run.length, index);
  }

  let masked = "";
  let copyFrom = 0;
  for (let index = 0; index < runs.length; index += 1) {
    const open = runs[index];
    const closeIndex = nextSameRun[index];
    if (open.start < copyFrom || closeIndex < 0) continue;
    const close = runs[closeIndex];
    masked += content.slice(copyFrom, open.start);
    masked += " ".repeat(close.end - open.start);
    copyFrom = close.end;
    index = closeIndex;
  }

  return copyFrom ? masked + content.slice(copyFrom) : content;
}

function removeCodeForMathDetection(content) {
  let fenceCharacter = "";
  let fenceLength = 0;
  const visibleLines = [];

  for (const line of content.split("\n")) {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/);
    if (fenceCharacter) {
      const closing = line.match(/^ {0,3}(`+|~+)\s*$/);
      if (closing && closing[1][0] === fenceCharacter && closing[1].length >= fenceLength) {
        fenceCharacter = "";
        fenceLength = 0;
      }
      visibleLines.push("");
      continue;
    }
    if (marker) {
      fenceCharacter = marker[1][0];
      fenceLength = marker[1].length;
      visibleLines.push("");
      continue;
    }
    if (/^(?: {4}|\t)/.test(line)) {
      visibleLines.push("");
      continue;
    }
    visibleLines.push(line);
  }

  return maskInlineCodeForMathDetection(visibleLines.join("\n"));
}

export function containsMathSource(content) {
  const source = removeCodeForMathDetection(String(content || ""));
  if (containsPairedDollarMath(source)) return true;
  if (/\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\]|\\begin\{([a-z]+)\}[\s\S]+?\\end\{\1\}/.test(source)) return true;

  if (!source.includes("[") || !source.includes("\\") || !source.includes("]")) return false;
  const normalized = normalizeDisplayMathDelimiters(source);
  return normalized !== source && containsPairedDollarMath(normalized);
}

/** Remove the faux byline/save-status row some models add as document chrome. */
export function stripDocumentChrome(content) {
  const lines = String(content || "").split(/\r?\n/);
  const compact = line => line
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/&(?:nbsp|#160|#x0*a0);/gi, " ")
    .replace(/<[^>]*>/g, "")
    .replace(/^\s{0,3}(?:>\s*)+/, "")
    .replace(/[\s*_`~|｜·•:：\u200B-\u200D\uFEFF]/g, "")
    .replace(/\p{Extended_Pictographic}/gu, "");
  const isMetadataRow = line => /^(?:你)?我的工作区(?:自动保存(?:到当前对话)?|保存到当前对话)$/.test(compact(line));
  const isWorkspaceLabel = line => /^(?:你)?我的工作区$/.test(compact(line));
  const isSaveLabel = line => /^(?:自动保存(?:到当前对话)?|保存到当前对话)$/.test(compact(line));
  const isAuthorBadge = line => {
    const value = compact(line);
    return value === "你" || (!value && /!\[[^\]]*\]\(|<img\b|\p{Extended_Pictographic}/iu.test(line));
  };
  const remove = new Set();

  lines.forEach((line, index) => {
    if (!isMetadataRow(line) && !isWorkspaceLabel(line)) return;
    if (isMetadataRow(line)) remove.add(index);

    // Some model responses split the faux byline into separate Markdown lines,
    // e.g. "你", "我的工作区", and "自动保存". Only remove that exact small
    // cluster so ordinary prose mentioning these words remains untouched.
    if (isWorkspaceLabel(line)) {
      for (const direction of [-1, 1]) {
        let adjacent = index + direction;
        let skipped = 0;
        while (adjacent >= 0 && adjacent < lines.length && skipped <= 2) {
          if (!lines[adjacent].trim() || isAuthorBadge(lines[adjacent])) {
            adjacent += direction;
            skipped += 1;
            continue;
          }
          if (isSaveLabel(lines[adjacent])) {
            remove.add(index);
            remove.add(adjacent);
            break;
          }
          break;
        }
      }
    }

    if (!remove.has(index)) return;
    for (const direction of [-1, 1]) {
      let adjacent = index + direction;
      let skipped = 0;
      while (adjacent >= 0 && adjacent < lines.length && skipped <= 2) {
        if (!lines[adjacent].trim()) {
          adjacent += direction;
          skipped += 1;
          continue;
        }
        if (!isAuthorBadge(lines[adjacent])) break;
        remove.add(adjacent);
        adjacent += direction;
        skipped += 1;
      }
    }
  });

  const filtered = lines.filter((line, index) => !remove.has(index));
  return filtered.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}
