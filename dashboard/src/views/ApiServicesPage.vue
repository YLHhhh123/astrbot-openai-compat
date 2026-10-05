<script setup>
/**
 * API 服务页。
 *
 * 展示内核原生 OpenAI 兼容接口（/v1/*）的接入信息，并提供 Key 管理与在线测试。
 *
 * 鉴权边界（两类接口，规则不同）：
 * - **管理接口** `/api/v1/openai-compat/*`：管理 AstrBot 自身，
 *   必须携带 `sk-astrbot-` 前缀的 Key 才能执行管理操作；
 * - **服务接口** `/v1/*`：默认按 HTTP 服务对外提供，
 *   使用标准 OpenAI 客户端方式（`Authorization: Bearer <Key>`）调用。
 */
import { apiV1Client } from '@/api/http';
import { computed, onMounted, ref } from 'vue';

const loading = ref(false);
const keys = ref([]);
const config = ref({});
const status = ref({});

const createDialog = ref(false);
const newRemark = ref('');
const createdKey = ref('');

const snack = ref({ show: false, text: '', color: 'success' });

function notify(text, color = 'success') {
  snack.value = { show: true, text: String(text), color };
}

const origin = computed(() => (typeof window === 'undefined' ? '' : window.location.origin));
const baseUrl = computed(() => `${origin.value}/v1`);

const keyPrefixHint = 'sk-astrbot-';

/** 端点清单（与服务实际注册情况一致） */
const endpointGroups = ref([
  {
    title: '对话',
    items: [
      { method: 'POST', path: '/v1/chat/completions', desc: '对话补全（支持 SSE 流式与 function calling）' },
      { method: 'POST', path: '/v1/completions', desc: 'Legacy 文本补全' }
    ]
  },
  {
    title: '模型',
    items: [
      { method: 'GET', path: '/v1/models', desc: '模型列表（provider 实例 + 别名）' },
      { method: 'GET', path: '/v1/models/{model_id}', desc: '单个模型详情' }
    ]
  },
  {
    title: '向量与重排',
    items: [
      { method: 'POST', path: '/v1/embeddings', desc: '文本向量' },
      { method: 'POST', path: '/v1/rerank', desc: '重排（Jina / Cohere 风格）' }
    ]
  },
  {
    title: '音频',
    items: [
      { method: 'POST', path: '/v1/audio/transcriptions', desc: '语音识别' },
      { method: 'POST', path: '/v1/audio/translations', desc: '语音翻译' },
      { method: 'POST', path: '/v1/audio/speech', desc: '语音合成' }
    ]
  },
  {
    title: '其它',
    items: [
      { method: 'GET', path: '/v1/health', desc: '存活探测（免鉴权）' },
      { method: 'GET', path: '/api/v1/docs', desc: '交互式接口文档' }
    ]
  },
  {
    title: '占位（内核暂无对应后端，返回 501）',
    items: [
      { method: 'POST', path: '/v1/images/generations', desc: '图像生成' },
      { method: 'POST', path: '/v1/moderations', desc: '内容审核' },
      { method: 'POST', path: '/v1/responses', desc: 'Responses API' },
      { method: 'POST', path: '/v1/files', desc: '文件管理' }
    ]
  }
]);

const methodColor = (method) => {
  const map = { GET: 'primary', POST: 'success', PATCH: 'warning', DELETE: 'error' };
  return map[method] || 'grey';
};

async function loadAll() {
  loading.value = true;
  try {
    const [keysRes, cfgRes, statusRes] = await Promise.all([
      apiV1Client.get('/openai-compat/keys'),
      apiV1Client.get('/openai-compat/config'),
      apiV1Client.get('/openai-compat/status')
    ]);
    keys.value = keysRes?.data?.data || [];
    config.value = cfgRes?.data?.data || {};
    status.value = statusRes?.data?.data || {};
  } catch (error) {
    notify(`加载失败：${error?.response?.data?.message || error}`, 'error');
  } finally {
    loading.value = false;
  }
}

async function openCreate() {
  newRemark.value = '';
  createdKey.value = '';
  createDialog.value = true;
}

async function submitCreate() {
  try {
    const res = await apiV1Client.post('/openai-compat/keys', { remark: newRemark.value });
    const data = res?.data?.data || {};
    createdKey.value = data.key || data.plaintext || '';
    await loadAll();
    if (createdKey.value) {
      notify('已创建，请立即保存明文（仅显示这一次）');
    }
  } catch (error) {
    notify(`创建失败：${error?.response?.data?.message || error}`, 'error');
  }
}

async function toggleKey(item) {
  try {
    await apiV1Client.patch(`/openai-compat/keys/${item.id}`, { enabled: !item.enabled });
    await loadAll();
    notify(item.enabled ? '已停用' : '已启用');
  } catch (error) {
    notify(`操作失败：${error?.response?.data?.message || error}`, 'error');
  }
}

async function removeKey(item) {
  if (!window.confirm(`确定删除「${item.remark || item.key_prefix}」？删除后无法恢复。`)) return;
  try {
    await apiV1Client.delete(`/openai-compat/keys/${item.id}`);
    await loadAll();
    notify('已删除');
  } catch (error) {
    notify(`删除失败：${error?.response?.data?.message || error}`, 'error');
  }
}

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    notify('已复制');
  } catch {
    notify('复制失败，请手动选择', 'error');
  }
}

function fmtTime(value) {
  if (!value) return '从未';
  try {
    return new Date(value * 1000).toLocaleString();
  } catch {
    return String(value);
  }
}

onMounted(loadAll);
</script>

<template>
  <v-container fluid class="pa-4">
    <v-card class="mb-4">
      <v-card-title class="text-h6">API 服务</v-card-title>
      <v-card-subtitle>内核原生 OpenAI 兼容接口</v-card-subtitle>
      <v-card-text>
        <v-alert type="info" variant="tonal" density="compact" class="mb-4">
          <div class="text-subtitle-2 mb-1">鉴权规则</div>
          <ul class="ml-4 mt-1" style="line-height: 1.7">
            <li>
              <strong>管理接口</strong> <code>/api/v1/openai-compat/*</code>：用于管理 AstrBot 自身，
              <strong>必须携带 <code>{{ keyPrefixHint }}</code> 前缀的 Key</strong> 才能执行管理操作。
            </li>
            <li>
              <strong>服务接口</strong> <code>/v1/*</code>：默认按 HTTP 服务对外提供，
              使用标准 OpenAI 客户端方式调用（<code>Authorization: Bearer &lt;Key&gt;</code>）。
            </li>
          </ul>
        </v-alert>

        <div class="text-subtitle-2 mb-1">服务地址（Base URL）</div>
        <v-text-field
          :model-value="baseUrl"
          readonly
          density="compact"
          variant="outlined"
          append-inner-icon="mdi-content-copy"
          @click:append-inner="copy(baseUrl)"
        />
        <div class="text-caption text-medium-emphasis">
          在 OpenAI 客户端（Cherry Studio、Operit 等）中填这个地址，API Key 填下方创建的 Key。
          当前服务状态：{{ status.enabled === false ? '已关闭' : '已开启' }}
        </div>
      </v-card-text>
    </v-card>

    <v-card class="mb-4">
      <v-card-title class="text-h6 d-flex align-center justify-space-between">
        <span>API Key</span>
        <v-btn color="primary" size="small" prepend-icon="mdi-plus" @click="openCreate">新建 Key</v-btn>
      </v-card-title>
      <v-card-text>
        <v-alert type="warning" variant="tonal" density="compact" class="mb-3">
          Key 明文仅在创建时显示一次，请立即保存。所有 Key 均以 <code>{{ keyPrefixHint }}</code> 开头。
        </v-alert>

        <v-table density="compact" :loading="loading">
          <thead>
            <tr>
              <th>备注</th>
              <th>前缀</th>
              <th>状态</th>
              <th class="text-right">请求数</th>
              <th>最后使用</th>
              <th class="text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!keys.length">
              <td colspan="6" class="text-center text-medium-emphasis py-4">还没有 Key，点击「新建 Key」创建</td>
            </tr>
            <tr v-for="item in keys" :key="item.id">
              <td>{{ item.remark || '（无备注）' }}</td>
              <td><code>{{ item.key_prefix }}…</code></td>
              <td>
                <v-chip size="x-small" :color="item.enabled ? 'success' : 'grey'" variant="tonal">
                  {{ item.enabled ? '启用' : '停用' }}
                </v-chip>
              </td>
              <td class="text-right">{{ item.request_count ?? 0 }}</td>
              <td class="text-caption">{{ fmtTime(item.last_used_at) }}</td>
              <td class="text-right">
                <v-btn size="x-small" variant="text" @click="toggleKey(item)">
                  {{ item.enabled ? '停用' : '启用' }}
                </v-btn>
                <v-btn size="x-small" variant="text" color="error" @click="removeKey(item)">删除</v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>
      </v-card-text>
    </v-card>

    <v-card class="mb-4">
      <v-card-title class="text-h6">接口清单</v-card-title>
      <v-card-text>
        <div v-for="group in endpointGroups" :key="group.title" class="mb-4">
          <div class="text-subtitle-2 mb-2">{{ group.title }}</div>
          <v-table density="compact">
            <tbody>
              <tr v-for="ep in group.items" :key="ep.method + ep.path">
                <td style="width: 90px">
                  <v-chip size="x-small" :color="methodColor(ep.method)" variant="flat">{{ ep.method }}</v-chip>
                </td>
                <td style="width: 320px"><code>{{ ep.path }}</code></td>
                <td class="text-caption text-medium-emphasis">{{ ep.desc }}</td>
              </tr>
            </tbody>
          </v-table>
        </div>
      </v-card-text>
    </v-card>

    <v-dialog v-model="createDialog" max-width="560">
      <v-card>
        <v-card-title class="text-h6">新建 API Key</v-card-title>
        <v-card-text>
          <v-text-field v-model="newRemark" label="备注（可选）" density="compact" variant="outlined" />
          <template v-if="createdKey">
            <v-alert type="success" variant="tonal" density="compact" class="mt-2">
              <div class="text-subtitle-2 mb-1">请立即保存（仅显示一次）</div>
              <code style="word-break: break-all">{{ createdKey }}</code>
            </v-alert>
          </template>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="createDialog = false">关闭</v-btn>
          <v-btn v-if="!createdKey" color="primary" variant="flat" @click="submitCreate">创建</v-btn>
          <v-btn v-else color="primary" variant="flat" @click="copy(createdKey)">复制</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snack.show" :color="snack.color" timeout="2600">
      {{ snack.text }}
    </v-snackbar>
  </v-container>
</template>
