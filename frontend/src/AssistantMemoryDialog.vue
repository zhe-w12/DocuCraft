<script setup>
import { ref } from "vue";

const props = defineProps({
  apiBase: { type: String, default: "" },
  profileId: { type: String, default: "" },
});

const dialog = ref(null);
const memories = ref([]);
const loading = ref(false);
const error = ref("");
const deletingId = ref("");
const clearing = ref(false);

function requestUrl(path) {
  return `${String(props.apiBase || "").trim().replace(/\/+$/, "")}${path}`;
}

function profileHeaders() {
  return { "X-DocuCraft-Memory-Profile": props.profileId };
}

async function parseResponse(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "长期记忆暂时无法读取");
  return data;
}

async function loadMemories() {
  if (!props.profileId) {
    error.value = "当前浏览器无法保存记忆标识，请检查本地存储权限。";
    memories.value = [];
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    const response = await fetch(requestUrl("/api/v1/assistant/memories"), {
      headers: profileHeaders(),
      cache: "no-store",
    });
    const data = await parseResponse(response);
    memories.value = Array.isArray(data.memories) ? data.memories : [];
  } catch (requestError) {
    error.value = requestError?.message || "长期记忆暂时无法读取";
  } finally {
    loading.value = false;
  }
}

async function open() {
  error.value = "";
  if (!dialog.value?.open) dialog.value?.showModal();
  await loadMemories();
}

async function deleteMemory(memoryId) {
  deletingId.value = memoryId;
  error.value = "";
  try {
    const response = await fetch(requestUrl(`/api/v1/assistant/memories/${encodeURIComponent(memoryId)}`), {
      method: "DELETE",
      headers: profileHeaders(),
    });
    await parseResponse(response);
    memories.value = memories.value.filter(memory => memory.id !== memoryId);
  } catch (requestError) {
    error.value = requestError?.message || "删除记忆失败";
  } finally {
    deletingId.value = "";
  }
}

async function clearMemories() {
  if (!memories.value.length || !globalThis.confirm("确定清空这个浏览器档案的全部长期记忆吗？")) return;
  clearing.value = true;
  error.value = "";
  try {
    const response = await fetch(requestUrl("/api/v1/assistant/memories"), {
      method: "DELETE",
      headers: profileHeaders(),
    });
    await parseResponse(response);
    memories.value = [];
  } catch (requestError) {
    error.value = requestError?.message || "清空记忆失败";
  } finally {
    clearing.value = false;
  }
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

function categoryLabel(value) {
  return ({ semantic: "事实与偏好", episodic: "重要事件", procedural: "做事习惯" })[value] || "事实与偏好";
}

defineExpose({ open });
</script>

<template>
  <dialog ref="dialog" class="assistant-memory-dialog" aria-labelledby="assistantMemoryTitle">
    <div class="assistant-memory-heading">
      <div>
        <span class="section-kicker">LONG-TERM MEMORY</span>
        <h2 id="assistantMemoryTitle">长期记忆</h2>
      </div>
      <div class="assistant-memory-heading-actions">
        <button class="ghost-button" type="button" :disabled="loading" @click="loadMemories">刷新</button>
        <button class="icon-button" type="button" aria-label="关闭长期记忆" @click="dialog?.close()">×</button>
      </div>
    </div>
    <p class="assistant-memory-note">AI 仅将适合跨对话使用的偏好和项目事实写入服务器数据库；不会保存密钥或敏感信息。记忆按当前浏览器档案隔离，你可以逐条删除或全部清空。</p>
    <div v-if="error" class="assistant-inline-error assistant-memory-error" role="alert">{{ error }}</div>
    <div v-if="loading" class="assistant-memory-empty" role="status">正在读取记忆…</div>
    <div v-else-if="!memories.length" class="assistant-memory-empty">还没有长期记忆。你可以告诉 AI “记住：……”来保存信息。</div>
    <ul v-else class="assistant-memory-list">
      <li v-for="memory in memories" :key="memory.id" class="assistant-memory-row">
        <div>
          <p>{{ memory.content }}</p>
          <small>{{ categoryLabel(memory.category) }}<span v-if="formatDate(memory.created_at)"> · {{ formatDate(memory.created_at) }}</span></small>
        </div>
        <button class="danger-text-button" type="button" :disabled="Boolean(deletingId) || clearing" @click="deleteMemory(memory.id)">
          {{ deletingId === memory.id ? "删除中…" : "删除" }}
        </button>
      </li>
    </ul>
    <div class="assistant-memory-actions">
      <button v-if="memories.length" class="danger-text-button" type="button" :disabled="clearing || Boolean(deletingId)" @click="clearMemories">{{ clearing ? "清空中…" : "清空全部" }}</button>
      <button class="ghost-button" type="button" @click="dialog?.close()">完成</button>
    </div>
  </dialog>
</template>
