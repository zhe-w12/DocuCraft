const global = window;

  const DATABASE_NAME = "iscan-local-documents";
  const STORE_NAME = "pdfs";
  let connection = null;
  let opening = null;

  function storageError(message, name) {
    return new DOMException(message, name);
  }

  function forgetConnection(database) {
    if (connection === database) connection = null;
  }

  function openDatabase() {
    if (connection) return Promise.resolve(connection);
    if (opening) return opening;

    const pending = new Promise((resolve, reject) => {
      let request;
      let settled = false;
      const fail = (error) => {
        if (settled) return;
        settled = true;
        reject(error);
      };

      try {
        if (!global.indexedDB) {
          fail(storageError("此浏览器无法访问本地文件存储。", "NotSupportedError"));
          return;
        }
        request = global.indexedDB.open(DATABASE_NAME, 1);
      } catch (error) {
        fail(error);
        return;
      }

      request.onblocked = () => {
        fail(storageError("本地文件存储被其他页面占用，请关闭其他 DocuCraft 页面后重试。", "InvalidStateError"));
      };
      request.onerror = () => {
        fail(request.error || storageError("无法打开本地文件存储。", "UnknownError"));
      };
      request.onupgradeneeded = () => {
        // An open request cannot be cancelled after it has been blocked.
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
        const database = request.result;
        if (settled) {
          database.close();
          return;
        }
        database.onversionchange = () => {
          forgetConnection(database);
          database.close();
        };
        database.onclose = () => forgetConnection(database);
        connection = database;
        settled = true;
        resolve(database);
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
    let database = await openDatabase();
    let transaction;
    try {
      transaction = database.transaction(STORE_NAME, mode);
    } catch (error) {
      // A version change can close the connection between await and transaction.
      if (error.name !== "InvalidStateError") throw error;
      forgetConnection(database);
      database.close();
      database = await openDatabase();
      transaction = database.transaction(STORE_NAME, mode);
    }

    return new Promise((resolve, reject) => {
      let result;
      let failure;
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = (event) => {
        failure = failure || event.target.error || transaction.error;
        // Keep the default error action, which aborts every write in the batch.
      };
      transaction.onabort = () => {
        reject(failure || transaction.error || storageError("本地文件存储操作已中止。", "AbortError"));
      };

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

  function validateId(id) {
    if (typeof id !== "string" || !id) throw new TypeError("本地文件需要有效的 ID。");
  }

export const LocalDocuments = Object.freeze({
    async put(id, file) {
      validateId(id);
      if (!(file instanceof global.Blob) || typeof file.name !== "string") {
        throw new TypeError("需要提供包含文件名的原始文件。");
      }
      await transact("readwrite", (store) => store.put({
        id,
        blob: file,
        name: file.name,
        lastModified: file.lastModified,
      }));
    },

    async get(id) {
      validateId(id);
      return transact("readonly", (store) => store.get(id));
    },

    async remove(id) {
      validateId(id);
      await transact("readwrite", (store) => store.delete(id));
    },

    async removeMany(ids) {
      if (!Array.isArray(ids)) throw new TypeError("需要提供文件 ID 列表。");
      const keys = ids.slice();
      keys.forEach(validateId);
      await transact("readwrite", (store) => {
        keys.forEach((id) => store.delete(id));
      });
    },
  });
