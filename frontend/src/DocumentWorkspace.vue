<script setup>
defineProps({
  title: { type: String, default: "" },
  details: { type: String, default: "" },
  showHeader: { type: Boolean, default: true },
});
</script>

<template>
  <div class="document-workspace">
    <header v-if="showHeader" class="document-workspace-header">
      <div class="document-workspace-file">
        <strong :title="title">{{ title }}</strong>
        <span v-if="details">{{ details }}</span>
      </div>
      <div class="document-workspace-actions"><slot name="actions" /></div>
    </header>
    <div v-if="$slots.toolbar" class="document-workspace-toolbar"><slot name="toolbar" /></div>
    <main class="document-workspace-canvas"><slot /></main>
  </div>
</template>
