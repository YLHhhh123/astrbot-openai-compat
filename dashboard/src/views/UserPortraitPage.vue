<script setup>
/**
 * 用户形象页。
 *
 * 三块内容：
 *  1. 形象推理开关与参数（空闲触发、样本门限、每日次数、用哪个模型分组）
 *  2. 多轮思考开关与参数（轮数、发言方式、间隔）
 *  3. 已推理的形象列表 + 详情（形象、置信度、证据、推理流水）
 *
 * 与内核的「AI 人格档案」（/personas，PersonaPage）不是一回事：
 * 那管的是胡桃自己怎么演，这个人形象页管的是"它眼里的用户是谁"。
 */
import { apiV1Client } from '@/api/http';
import { computed, onMounted, ref } from 'vue';

const loading = ref(false);
const busy = ref(false);
const settings = ref({ persona: {}, reflection: {} });
const list = ref([]);
const detail = ref(null);
const logs = ref([]);
const keyword = ref('');
const search = ref('');
const snack = ref({ show: false, text: '', color: 'success' });

const MODE_HINT = {
  off: '完全关闭',
  internal: '完整思考后只发最终版',
  progressive: '每轮思考后发一条，渐进对话',
  with_thinking: '连思考摘要一起发',
};

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase();
  if (!q) return list.value;
  return list.value.filter(
    (p) =>
      (p.user_id || '').toLowerCase().includes(q) ||
      (p.summary || '').toLowerCase().includes(q),
  );
});

const stat = computed(() => ({
  total: list.value.length,
  enough: list.value.filter((p) => (p.sample_count || 0) >= 20).length,
  fresh: list.value.filter((p) => Date.now() / 1000 - (p.updated_at || 0) < 86400).length,
}));

function notify(text, color = 'success') {
  snack.value = { show: true, text: String(text), color };
}

async function loadSettings() {
  const res = await apiV1Client.get('/persona/settings');
  settings.value = res?.data?.data || settings.value;
}

async function loadList() {
  const res = await apiV1Client.get('/persona/list', { params: { limit: 200 } });
  list.value = res?.data?.data || [];
}

async function reload() {
  loading.value = true;
  try {
    await Promise.all([loadSettings(), loadList()]);
  } catch (e) {
    notify(`加载失败：${e?.response?.data?.detail || e?.message || e}`, 'error');
  } finally {
    loading.value = false;
  }
}

async function savePersona(patch) {
  busy.value = true;
  try {
    await apiV1Client.put('/persona/settings', patch);
    await loadSettings();
    notify('已保存');
  } catch (e) {
    notify(`保存失败：${e?.response?.data?.detail || e?.message || e}`, 'error');
  } finally {
    busy.value = false;
  }
}

async function saveReflection(patch) {
  busy.value = true;
  try {
    await apiV1Client.put('/persona/reflection', patch);
    await loadSettings();
    notify('已保存');
  } catch (e) {
    notify(`保存失败：${e?.response?.data?.detail || e?.message || e}`, 'error');
  } finally {
    busy.value = false;
  }
}

async function openDetail(userId) {
  try {
    const [d, l] = await Promise.all([
      apiV1Client.get(`/persona/${encodeURIComponent(userId)}`),
      apiV1Client.get(`/persona/${encodeURIComponent(userId)}/logs`),
    ]);
    detail.value = d?.data?.data || null;
    logs.value = l?.data?.data || [];
  } catch (e) {
    notify(`读取失败：${e?.message || e}`, 'error');
  }
}

async function infer(userId) {
  busy.value = true;
  try {
    const res = await apiV1Client.post('/persona/infer', { user_id: userId });
    const d = res?.data?.data || {};
    if (d.ok) {
      notify(`推理完成：置信度 ${d.confidence}，样本 ${d.sample_count}`);
      await loadList();
      await openDetail(userId);
    } else {
      notify(`未产出：${d.reason || '未知原因'}`, 'warning');
      await openDetail(userId);
    }
  } catch (e) {
    notify(`推理失败：${e?.response?.data?.detail || e?.message || e}`, 'error');
  } finally {
    busy.value = false;
  }
}

async function remove(userId) {
  if (!window.confirm(`确定删除 ${userId} 的形象？不可撤销。`)) return;
  busy.value = true;
  try {
    await apiV1Client.delete(`/persona/${encodeURIComponent(userId)}`);
    notify('已删除');
    detail.value = null;
    await loadList();
  } catch (e) {
    notify(`删除失败：${e?.message || e}`, 'error');
  } finally {
    busy.value = false;
  }
}

function asList(v) {
  return Array.isArray(v) ? v : v ? [v] : [];
}

function confColor(c) {
  if (c >= 0.8) return 'success';
  if (c >= 0.6) return 'info';
  return 'warning';
}

function ts(v) {
  return v ? new Date(v * 1000).toLocaleString() : '';
}

onMounted(reload);
</script>

<template>
  <v-container fluid class="pa-4">
    <v-card class="mb-4">
      <v-card-title class="text-h6">形象推理</v-card-title>
      <v-card-subtitle>对话空闲后读取聊天记录，推理用户形象</v-card-subtitle>
      <v-card-text>
        <v-alert type="info" variant="tonal" density="compact" class="mb-4">
          样本不足 {{ settings.persona.min_samples ?? 20 }} 条时<strong>不会产出结论</strong>
          ——宁可没有，也不编造。置信度按样本量分档，上限 0.95（画像是概括，不是事实）。
        </v-alert>

        <v-switch
          :model-value="settings.persona.enabled"
          :disabled="busy"
          color="primary"
          hide-details
          label="启用自动推理"
          @update:model-value="(v) => savePersona({ enabled: !!v })"
        />

        <v-row class="mt-4" dense>
          <v-col cols="12" md="3">
            <v-text-field
              :model-value="settings.persona.idle_minutes"
              label="空闲多久触发（分钟）"
              type="number"
              density="compact"
              variant="outlined"
              :disabled="busy"
              @update:model-value="(v) => savePersona({ idle_minutes: Number(v) || 10 })"
            />
          </v-col>
          <v-col cols="12" md="3">
            <v-text-field
              :model-value="settings.persona.min_samples"
              label="最少样本条数"
              type="number"
              density="compact"
              variant="outlined"
              :disabled="busy"
              hint="低于此值不推理"
              persistent-hint
              @update:model-value="(v) => savePersona({ min_samples: Number(v) || 20 })"
            />
          </v-col>
          <v-col cols="12" md="3">
            <v-text-field
              :model-value="settings.persona.max_per_day"
              label="每用户每日上限"
              type="number"
              density="compact"
              variant="outlined"
              :disabled="busy"
              @update:model-value="(v) => savePersona({ max_per_day: Number(v) || 3 })"
            />
          </v-col>
          <v-col cols="12" md="3">
            <v-text-field
              :model-value="settings.persona.model_group"
              label="推理用模型分组"
              density="compact"
              variant="outlined"
              :disabled="busy"
              placeholder="留空 = 用当前默认模型"
              clearable
              @update:model-value="(v) => savePersona({ model_group: v || '' })"
            />
          </v-col>
        </v-row>
      </v-card-text>
    </v-card>

    <v-card class="mb-4">
      <v-card-title class="text-h6">多轮思考</v-card-title>
      <v-card-subtitle>让 AI 先想、再想、然后说，像人一样说话</v-card-subtitle>
      <v-card-text>
        <v-alert type="warning" variant="tonal" density="compact" class="mb-4">
          每轮多调一次模型，<strong>成本按轮数翻倍</strong>。建议先用 2 轮观察效果。
          思考轮失败会自动沿用上一版，不会让用户收不到回复。
        </v-alert>

        <v-switch
          :model-value="settings.reflection.enabled"
          :disabled="busy"
          color="primary"
          hide-details
          label="启用多轮思考"
          @update:model-value="(v) => saveReflection({ enabled: !!v })"
        />

        <v-row class="mt-4" dense>
          <v-col cols="12" md="3">
            <v-select
              :model-value="settings.reflection.rounds"
              label="思考轮数"
              :items="[
                { title: '1（不反思）', value: 1 },
                { title: '2（推荐）', value: 2 },
                { title: '3', value: 3 },
                { title: '4', value: 4 },
              ]"
              density="compact"
              variant="outlined"
              :disabled="busy"
              @update:model-value="(v) => saveReflection({ rounds: Number(v) })"
            />
          </v-col>
          <v-col cols="12" md="3">
            <v-select
              :model-value="settings.reflection.mode"
              label="发言方式"
              :items="[
                { title: '边想边说（推荐）', value: 'progressive' },
                { title: '只发最终版', value: 'internal' },
                { title: '连思考一起发', value: 'with_thinking' },
                { title: '关闭', value: 'off' },
              ]"
              density="compact"
              variant="outlined"
              :disabled="busy"
              :hint="MODE_HINT[settings.reflection.mode]"
              persistent-hint
              @update:model-value="(v) => saveReflection({ mode: v })"
            />
          </v-col>
          <v-col cols="12" md="3">
            <v-text-field
              :model-value="settings.reflection.send_interval"
              label="发言间隔（秒）"
              type="number"
              step="0.5"
              density="compact"
              variant="outlined"
              :disabled="busy"
              @update:model-value="(v) => saveReflection({ send_interval: Number(v) })"
            />
          </v-col>
          <v-col cols="12" md="3">
            <v-text-field
              :model-value="settings.reflection.model_group"
              label="思考用模型分组"
              density="compact"
              variant="outlined"
              :disabled="busy"
              placeholder="留空 = 用当前默认模型"
              clearable
              hint="可用小模型省钱"
              persistent-hint
              @update:model-value="(v) => saveReflection({ model_group: v || '' })"
            />
          </v-col>
        </v-row>
      </v-card-text>
    </v-card>

    <v-card>
      <v-card-title class="text-subtitle-1">
        已推理的形象
        <v-btn icon="mdi-refresh" size="small" variant="text" class="ml-2" @click="reload" />
      </v-card-title>
      <v-card-subtitle>
        共 {{ stat.total }} 人 ·样本达标 {{ stat.enough }} 人 · 24 小时内更新 {{ stat.fresh }} 人
      </v-card-subtitle>
      <v-card-text>
        <v-text-field
          v-model="search"
          placeholder="搜索用户 ID 或形象关键词"
          prepend-inner-icon="mdi-magnify"
          density="compact"
          variant="outlined"
          clearable
          hide-details
          class="mb-3"
        />

        <v-skeleton-loader v-if="loading" type="table" />
        <v-table v-else-if="filtered.length" density="comfortable">
          <thead>
            <tr>
              <th style="width: 150px">用户</th>
              <th>形象</th>
              <th style="width: 110px">置信度</th>
              <th style="width: 80px">样本</th>
              <th style="width: 165px">更新时间</th>
              <th style="width: 200px"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in filtered" :key="p.user_id">
              <td class="font-weight-medium">{{ p.user_id }}</td>
              <td>
                <div>{{ p.summary || '（尚无结论）' }}</div>
                <div v-if="p.traits" class="mt-1">
                  <v-chip
                    v-for="(tags, dim) in p.traits"
                    :key="dim"
                    size="x-small"
                    variant="tonal"
                    class="mr-1 mt-1"
                  >
                    {{ dim }}：{{ asList(tags).join('、') }}
                  </v-chip>
                </div>
              </td>
              <td>
                <v-chip :color="confColor(p.confidence)" size="small" variant="tonal">
                  {{ (p.confidence ?? 0).toFixed(2) }}
                </v-chip>
              </td>
              <td class="text-body-2">{{ p.sample_count }}</td>
              <td class="text-body-2">{{ ts(p.updated_at) }}</td>
              <td class="text-right">
                <v-btn size="small" variant="text" @click="openDetail(p.user_id)">详情</v-btn>
                <v-btn
                  size="small" variant="text" color="primary"
                  :loading="busy" @click="infer(p.user_id)"
                >
                  重新推理
                </v-btn>
                <v-btn
                  size="small" variant="text" color="error"
                  :loading="busy" @click="remove(p.user_id)"
                >
                  删除
                </v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>
        <v-alert v-else type="info" variant="tonal" density="compact">
          还没有推理出形象。开启自动推理后会在用户空闲时自动推理；也可在下方填用户 ID 手动触发。
        </v-alert>

        <v-divider class="my-4" />
        <v-row align="center">
          <v-col cols="12" md="6">
            <v-text-field
              v-model="keyword"
              label="手动推理：输入用户 ID"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
          <v-col cols="12" md="3">
            <v-btn
              color="primary" variant="tonal" block
              :loading="busy"
              :disabled="!keyword || !keyword.trim()"
              @click="infer(keyword.trim())"
            >
              立即推理
            </v-btn>
          </v-col>
        </v-row>
      </v-card-text>
    </v-card>

    <v-dialog v-model="detail" max-width="820" scrollable>
      <v-card v-if="detail?.persona">
        <v-card-title class="text-subtitle-1">{{ detail.persona.user_id }} 的形象</v-card-title>
        <v-card-subtitle>
          更新于 {{ ts(detail.persona.updated_at) }} · 样本 {{ detail.persona.sample_count }} ·
          模型 {{ detail.persona.persona_model_id || '默认' }}
        </v-card-subtitle>
        <v-card-text>
          <v-alert
            v-if="!detail.persona.summary"
            type="warning" variant="tonal" density="compact"
          >
            样本量不足，尚未得出结论。
          </v-alert>
          <template v-else>
            <p class="text-h6 mb-3">{{ detail.persona.summary }}</p>

            <v-progress-linear
              :model-value="(detail.persona.confidence ?? 0) * 100"
              :color="confColor(detail.persona.confidence)"
              height="8"
              rounded
              class="mb-1"
            />
            <div class="text-caption mb-4">
              置信度 {{ (detail.persona.confidence ?? 0).toFixed(2) }} —— 上限 0.95，永远不会到 1.00
            </div>

            <h6 class="text-subtitle-2 mb-2">特质</h6>
            <div class="mb-4">
              <v-chip
                v-for="(tags, dim) in detail.persona.traits"
                :key="dim"
                size="small" variant="tonal" class="mr-2 mt-1"
              >
                <strong class="mr-1">{{ dim }}</strong>{{ asList(tags).join('、') }}
              </v-chip>
            </div>

            <h6 class="text-subtitle-2 mb-2">证据（原话摘录）</h6>
            <v-list density="compact" class="rounded mb-4" style="border: 1px solid rgba(127,127,127,.3)">
              <v-list-item v-for="(ev, i) in asList(detail.persona.evidence)" :key="i">
                <v-list-item-text>
                  <span v-if="typeof ev === 'string'">「{{ ev }}」</span>
                  <span v-else>
                    「{{ ev.text }}」<span v-if="ev.sender"> —— {{ ev.sender }}</span>
                  </span>
                </v-list-item-text>
              </v-list-item>
              <v-list-item v-if="!asList(detail.persona.evidence).length">
                <v-list-item-text class="text-medium-emphasis">
                  模型未给出引用 —— 这条结论缺少支撑，建议重新推理或人工复核。
                </v-list-item-text>
              </v-list-item>
            </v-list>
          </template>

          <template v-if="(detail.adjusts || []).length">
            <h6 class="text-subtitle-2 mb-2">群内修正项</h6>
            <v-table density="compact">
              <thead>
                <tr><th>群</th><th>补充说明</th><th style="width: 80px">样本</th></tr>
              </thead>
              <tbody>
                <tr v-for="a in detail.adjusts" :key="a.group_id">
                  <td>{{ a.group_id }}</td>
                  <td>{{ a.note || '—' }}</td>
                  <td>{{ a.sample_count }}</td>
                </tr>
              </tbody>
            </v-table>
          </template>

          <h6 class="text-subtitle-2 mt-5 mb-2">推理流水</h6>
          <v-table density="compact">
            <thead>
              <tr><th style="width: 155px">时间</th><th style="width: 80px">结果</th><th>说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="l in logs" :key="l.id">
                <td class="text-caption">{{ ts(l.created_at) }}</td>
                <td>
                  <v-chip :color="l.ok ? 'success' : 'grey'" size="x-small" variant="tonal">
                    {{ l.ok ? '成功' : '跳过' }}
                  </v-chip>
                </td>
                <td class="text-caption">
                  {{ l.reason || `样本 ${l.sample_count}` }}
                  <span v-if="l.elapsed_ms"> · {{ l.elapsed_ms }}ms</span>
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="detail = null">关闭</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-snackbar v-model="snack.show" :color="snack.color" timeout="4500">
      {{ snack.text }}
    </v-snackbar>
  </v-container>
</template>