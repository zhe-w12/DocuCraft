<script setup>
defineProps({
  kind: { type: String, default: "document" },
});
</script>

<template>
  <svg class="file-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    <path d="M6 3h8l4 4v14H6zM14 3v5h5" />
    <template v-if="kind === 'pdf'">
      <path d="M8.5 12h1.7a1.3 1.3 0 0 1 0 2.6H8.5V12Zm0 2.6v2.2m4-4.8v4.8h1a2.4 2.4 0 0 0 0-4.8h-1Zm5 0v4.8m0-4.8h2.5m-2.5 2.2h2" />
    </template>
    <template v-else-if="kind === 'table'">
      <path d="M8 11h8M8 14h8M8 17h8M11 10v8m3-8v8" />
    </template>
    <template v-else-if="kind === 'image'">
      <rect x="8" y="10" width="9" height="7" rx="1" />
      <circle cx="10.5" cy="12" r=".8" />
      <path d="m8.5 16 2.5-2.5 2 1.7 1.5-1.2 2 2" />
    </template>
    <template v-else>
      <path d="M8 12h8M8 15h8M8 18h6" />
    </template>
  </svg>
</template>

<style scoped>
.file-icon { display: block; fill: none; stroke: currentColor; stroke-width: 1.55; stroke-linecap: round; stroke-linejoin: round; }
</style>
