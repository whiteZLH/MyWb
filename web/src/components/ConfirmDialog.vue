<script setup>
import { ref, watch } from "vue";
import { closeConfirm, ui } from "../ui.js";
import { refresh, showToast } from "../store.js";

const dialog = ref(null);
const renameValue = ref("");
const error = ref("");
const busy = ref(false);

watch(
  () => ui.confirm.open,
  (open) => {
    if (open) {
      renameValue.value = ui.confirm.rename ?? "";
      error.value = "";
      dialog.value?.showModal();
    } else {
      dialog.value?.open && dialog.value.close();
    }
  }
);

async function onSubmit() {
  error.value = "";
  busy.value = true;
  try {
    const submit = ui.confirm.submit;
    const rename = ui.confirm.rename;
    await submit(rename !== null ? renameValue.value : undefined);
    closeConfirm();
    await refresh();
    showToast("操作已保存");
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <dialog ref="dialog" @close="closeConfirm">
    <form @submit.prevent="onSubmit">
      <h2>{{ ui.confirm.title }}</h2>
      <p class="muted">{{ ui.confirm.desc }}</p>
      <template v-if="ui.confirm.rename !== null">
        <label for="rename-value">账号备注</label>
        <input id="rename-value" v-model="renameValue" maxlength="60" />
      </template>
      <p class="field-error" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="secondary" @click="closeConfirm">取消</button>
        <button class="primary" type="submit" :disabled="busy">确认</button>
      </div>
    </form>
  </dialog>
</template>
