<script setup>
import { nextTick, ref, useId } from "vue";
import { focusElement } from "./focus-element.js";

const props = defineProps({
  title: { type: String, default: "自动复制不可用" },
  description: { type: String, default: "文本已选中，请按 Ctrl+C（Mac 按 ⌘+C）复制。" },
  label: { type: String, default: "要手动复制的文本" },
  selectButtonLabel: { type: String, default: "重新选择文字" },
});

const titleId = `manual-copy-title-${useId()}`;
const dialogElement = ref(null);
const textareaElement = ref(null);
const text = ref("");

async function open(value) {
  text.value = String(value ?? "");
  await nextTick();
  const dialog = dialogElement.value;
  if (!dialog) return false;

  if (!dialog.open) {
    try {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } catch {
      dialog.setAttribute("open", "");
    }
  }
  await nextTick();
  selectText();
  return true;
}

function selectText() {
  const textarea = textareaElement.value;
  if (!textarea) return;
  focusElement(textarea);
  textarea.select();
}

function close() {
  if (dialogElement.value?.open) dialogElement.value.close();
  else text.value = "";
}

function clear() {
  text.value = "";
}

defineExpose({ open, close, selectText });
</script>

<template>
  <dialog ref="dialogElement" class="assistant-manual-copy-dialog" :aria-labelledby="titleId" @close="clear">
    <div class="assistant-manual-copy-content">
      <h2 :id="titleId">{{ title }}</h2>
      <p>{{ description }}</p>
      <textarea ref="textareaElement" :value="text" readonly :aria-label="label" @focus="selectText"></textarea>
      <div class="assistant-manual-copy-actions">
        <button type="button" @click="selectText">{{ selectButtonLabel }}</button>
        <button type="button" class="is-primary" @click="close">完成</button>
      </div>
    </div>
  </dialog>
</template>
