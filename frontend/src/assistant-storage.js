const DATABASE_NAME = "iscan-assistant-storage";
const DATABASE_VERSION = 1;
const STORE_NAME = "snapshots";
const SNAPSHOT_ID = "conversations";

let database = null;
let opening = null;

function storageError(message, name = "UnknownError") {
  return new DOMException(message, name);
}

function forgetDatabase(candidate) {
  if (database === candidate) database = null;
}

function openDatabase() {
  if (database) return Promise.resolve(database);
  if (opening) return opening;

  const pending = new Promise((resolve, reject) => {
    let request;
    let settled = false;
    const fail = error => {
      if (settled) return;
      settled = true;
      reject(error);
    };

    try {
      if (!globalThis.indexedDB) {
        fail(storageError("此浏览器不支持本地对话存储。", "NotSupportedError"));
        return;
      }
      request = globalThis.indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    } catch (error) {
      fail(error);
      return;
    }

    request.onblocked = () => fail(storageError("本地对话存储被其他页面占用，请关闭其他 DocuCraft 页面后重试。", "InvalidStateError"));
    request.onerror = () => fail(request.error || storageError("无法打开本地对话存储。"));
    request.onupgradeneeded = () => {
      if (settled) {
        request.transaction.abort();
        return;
      }
      try {
        if (!request.result.objectStoreNames.contains(STORE_NAME)) {
          request.result.createObjectStore(STORE_NAME, { keyPath: "id" });
        }
      } catch (error) {
        request.transaction.abort();
        fail(error);
      }
    };
    request.onsuccess = () => {
      const candidate = request.result;
      if (settled) {
        candidate.close();
        return;
      }
      candidate.onversionchange = () => {
        forgetDatabase(candidate);
        candidate.close();
      };
      candidate.onclose = () => forgetDatabase(candidate);
      database = candidate;
      settled = true;
      resolve(candidate);
    };
  });

  opening = pending;
  const clearOpening = () => {
    if (opening === pending) opening = null;
  };
  pending.then(clearOpening, clearOpening);
  return pending;
}

async function transact(mode, enqueue) {
  const db = await openDatabase();
  let transaction;
  try {
    transaction = db.transaction(STORE_NAME, mode);
  } catch (error) {
    forgetDatabase(db);
    db.close();
    throw error;
  }

  return new Promise((resolve, reject) => {
    let result;
    let failure;
    transaction.oncomplete = () => resolve(result);
    transaction.onerror = event => {
      failure = failure || event.target.error || transaction.error;
    };
    transaction.onabort = () => reject(failure || transaction.error || storageError("本地对话存储操作已中止。", "AbortError"));

    try {
      const request = enqueue(transaction.objectStore(STORE_NAME));
      if (request) request.onsuccess = () => { result = request.result; };
    } catch (error) {
      failure = error;
      try {
        transaction.abort();
      } catch {
        reject(error);
      }
    }
  });
}

export const AssistantStorage = Object.freeze({
  async load() {
    return transact("readonly", store => store.get(SNAPSHOT_ID));
  },

  async save(snapshot) {
    return transact("readwrite", store => store.put({ ...snapshot, id: SNAPSHOT_ID }));
  },
});
