export async function commitHistoryBeforeFileMutation({
  persistNextHistory,
  restorePreviousHistory,
  mutateFiles,
}) {
  let historyPersisted = false;
  try {
    historyPersisted = persistNextHistory() !== false;
  } catch {
    historyPersisted = false;
  }

  if (!historyPersisted) {
    if (!restoreSafely(restorePreviousHistory)) {
      throw new Error("浏览器无法保存历史记录，且未能恢复原历史");
    }
    throw new Error("浏览器无法保存历史记录");
  }

  try {
    await mutateFiles?.();
  } catch (error) {
    if (!restoreSafely(restorePreviousHistory)) {
      throw new Error("文件操作失败，且历史记录回滚失败；请刷新后检查历史", { cause: error });
    }
    throw error;
  }
}

function restoreSafely(restorePreviousHistory) {
  try {
    return restorePreviousHistory() !== false;
  } catch {
    return false;
  }
}
