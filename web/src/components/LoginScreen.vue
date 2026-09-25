<script setup>
import { reactive, ref } from "vue";

// 传入函数 prop 而非事件：需要 await 父组件的登录请求以驱动按钮态与错误提示。
const props = defineProps({ submit: { type: Function, required: true } });

const form = reactive({ key: "" });
const error = ref("");
const busy = ref(false);

async function onSubmit() {
  error.value = "";
  busy.value = true;
  try {
    await props.submit(form.key);
    form.key = "";
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="login-screen">
    <div class="login-story">
      <a class="brand" href="/admin/">
        <span class="brand-symbol">M<span>↗</span></span>MYWB
        <span class="brand-label">CONSOLE</span>
      </a>
      <div>
        <span class="eyebrow light">YOUR MODELS, CONNECTED.</span>
        <h1>把连接打理好。<br /><em>让工作继续。</em></h1>
        <p>在一个地方管理登录凭据、客户端密钥<br />和你的模型连接。</p>
      </div>
      <footer><span class="status-dot"></span> 私有部署 · 你的工作空间</footer>
    </div>
    <div class="login-form-wrap">
      <form @submit.prevent="onSubmit">
        <span class="eyebrow">WORKSPACE ACCESS</span>
        <h2>登录管理控制台</h2>
        <p class="muted">使用独立管理密钥进入，客户端 API Key 不适用于此处。</p>
        <label for="admin-key">管理密钥</label>
        <input
          id="admin-key"
          v-model="form.key"
          type="password"
          autocomplete="current-password"
          placeholder="输入你的管理密钥"
          required
        />
        <p class="field-error" role="alert">{{ error }}</p>
        <button class="primary wide" type="submit" :disabled="busy">
          进入控制台 <span>↗</span>
        </button>
        <div class="privacy-note">
          <span>◇</span>
          <p>账号凭据保存在服务器。<br />浏览器不会保存你的管理密钥。</p>
        </div>
      </form>
      <span class="login-foot">MyWb · API Management</span>
    </div>
  </section>
</template>
