<script setup>
/**
 * 模块开关面板。
 *
 * 集中控制「上下文注入管线」这类编排插件：
 *   - 总开关
 *   - 逐阶段启停（写入插件配置文件，立即生效）
 *   - 查看各阶段绑定情况与上次执行耗时/错误
 *
 * 后端：/api/v1/module-switches/*
 */
import { apiV1Client } from '@/api/http';
import { computed, onMounted, ref } from 'vue';

const PLUGIN = 'astrbot_plugin_context_pipeline';

const loading = ref(false);
const saving = ref(false);
const rebinding = ref(false);
const notFound = ref(false);

const pipelineEnabled = ref(true);
const stages = ref([]);
const lastProblems = ref('');

// 本地编辑副本，点保存才提交
const draft = ref({});

const boundCount = computed(() => stages.value.filter((s) => s.bound).length);
const enabledCount = computed(() => stages.value.filter((s) => s.enabled).length);

const dirty = computed(() => {
  const d = draft.value;
  return (
    d.__total !== pipelineEnabled.value ||
    stages.value.some((s) => d[s.key] !== s.enabled)
  );
});

const unboundStages = computed(() => stages.value.filter((s) => !s.bound));

function syncDraft() {
  const d = { __total: pipelineEnabled.value };
  for (const s of stages.value) d[s.key] = s.enabled;
  draft.value = d;
}

async function load() {
  loading.value = true;
  notFound.value = false;
  try {
    const res = await apiV1Client.get(`/module-switches/${PLUGIN}`);
    const d = res?.data?.data || {};
    if (d.error) {
      notFound.value = true;
      stages.value = [];
      return;
    }
    pipelineEnabled.value = !!d.pipeline_enabled;
    lastProblems.value = d.last_problems || '';
    // 按 order 升序，保证展示顺序 = 实际执行顺序
    stages.value = [...(d.stages || [])].sort((a, b) => a.order - b.order);
    syncDraft();
  } catch (error) {
    notFound.value = true;
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const payload = {
      total_enabled: draft.value.__total,
      stages: stages.value.map((s) => ({ key: s.key, enabled: !!draft.value[s.key] })),
    };
    const res = await apiV1Client.put(`/module-switches/${PLUGIN}`, payload);
    const msg = res?.data?.message || '已保存';
    if (String(msg).includes('落盘失败')) {
      notify(msg, 'error');
    } else {
      notify(String(msg).includes('无变化') ? '配置无变化' : '已保存并生效');
    }
    await load();
  } catch (error) {
    notify(`保存失败：${error?.response?.data?.data?.error || error?.message || error}`, 'error');
  } finally {
    saving.value = false;
  }
}

async function rebind() {
  rebinding.value = true;
  try {
    const res = await apiV1Client.post(`/module-switches/${PLUGIN}/rebind`);
    const result = res?.data?.data?.result || {};
    const okCount = Object.values(result).filter((v) => v === '已绑定').length;
    notify(`已重新绑定 ${okCount}/${Object.keys(result).length} 个阶段`);
    await load();
  } catch (error) {
    notify(`重绑失败：${error?.message || error}`, 'error');
  } finally {
    rebinding.value = false;
  }
}

function reset() {
  syncDraft();
}

const snack = ref({ show: false, text: '', color: 'success' });
function notify(text, color = 'success') {
  snack.value = { show: true, text: String(text), color };
}

function statusChip(s) {
  if (!s.bound) return { text: '未绑定', color: 'grey' };
  if (s.last_error) return { text: '出错', color: 'error' };
  if (s.last_ran) return { text: '已执行', color: 'success' };
  if (s.last_skipped) return { text: '已跳过', color: 'warning' };
  return { text: '待命', color: 'info' };
}

onMounted(load);
</script>

<template>
  <v-container fluid class="pa-4">
    <v-card>
      <v-card-title class="text-h6 d-flex align-center">
        模块开关
        <v-chip size="small" class="ml-3" variant="tonal">
          上下文注入管线
        </v-chip>
      </v-card-title>
      <v-card-subtitle>
        集中控制各注入阶段的启停与执行顺序，取代多个插件各自抢on_llm_request 钩子
      </v-card-subtitle>

      <v-card-text>
        <v-alert v-if="notFound" type="warning" variant="tonal" density="compact" class="mb-4">
          未找到插件 <code>{{ PLUGIN }}</code>。请确认它已安装并启用，然后点右侧「重新绑定」。
        </v-alert>

        <v-skeleton-loader v-if="loading" type="card" />

        <template v-else-if="!notFound">
          <!-- 总开关 -->
          <v-switch
            v-model="draft.__total"
            color="primary"
            :label="`管线总开关（当前 ${enabledCount}/${stages.length} 个阶段启用）`"
            :hint="draft.__total ? '关闭后所有阶段都不执行' : '已关闭：所有注入阶段都不会运行'"
            persistent-hint
            density="comfortable"
          />

          <v-divider class="my-4" />

          <!-- 概览 -->
          <div class="d-flex flex-wrap ga-2 mb-4">
            <v-chip size="small" variant="tonal" color="success">
              已绑定 {{ boundCount }}/{{ stages.length }}
            </v-chip>
            <v-chip size="small" variant="tonal" color="info">
              启用 {{ enabledCount }}/{{ stages.length }}
            </v-chip>
            <v-chip v-if="lastProblems" size="small" variant="tonal" color="warning">
              {{ lastProblems }}
            </v-chip>
          </div>

          <v-alert
            v-if="unboundStages.length"
            type="info"
            variant="tonal"
            density="compact"
            class="mb-4"
          >
            以下阶段的目标插件未加载或未导出钩子，阶段会被跳过：
            <strong>{{ unboundStages.map((s) => s.label).join('、') }}</strong>
            <span class="text-caption">（启用/禁用它们仍然有效，绑定成功后立即生效）</span>
          </v-alert>

          <!-- 阶段表 -->
          <v-table density="compact" class="stage-table">
            <thead>
              <tr>
                <th style="width: 72px">顺序</th>
                <th style="width: 76px">启用</th>
                <th>阶段</th>
                <th style="width: 96px">状态</th>
                <th style="width: 96px">上次耗时</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in stages" :key="s.key">
                <td>
                  <v-chip size="x-small" variant="tonal">{{ s.order }}</v-chip>
                </td>
                <td>
                  <v-switch
                    v-model="draft[s.key]"
                    color="primary"
                    hide-details
                    density="compact"
                    :disabled="!draft.__total"
                  />
                </td>
                <td>
                  <div class="font-weight-medium">{{ s.label }}</div>
                  <div class="text-caption text-medium-emphasis">
                    <code>{{ s.key }}</code> · 来源 {{ s.source_plugin }}
                  </div>
                  <div v-if="s.description" class="text-caption mt-1">
                    {{ s.description }}
                  </div>
                  <div v-if="s.last_error" class="text-caption text-error mt-1">
                    {{ s.last_error }}
                  </div>
                  <div v-else-if="s.last_skipped" class="text-caption text-warning mt-1">
                    {{ s.last_skipped }}
                  </div>
                </td>
                <td>
                  <v-chip size="small" :color="statusChip(s).color" variant="tonal">
                    {{ statusChip(s).text }}
                  </v-chip>
                </td>
                <td class="text-caption">
                  {{ s.last_ran ? `${s.last_elapsed_ms} ms` : '—' }}
                </td>
              </tr>
            </tbody>
          </v-table>
        </template>
      </v-card-text>

      <v-card-actions>
        <v-btn
          variant="text"
          color="secondary"
          prepend-icon="mdi-link-variant"
          :loading="rebinding"
          @click="rebind"
        >
          重新绑定
        </v-btn>
        <v-spacer />
        <v-btn variant="text" :disabled="!dirty || saving" @click="reset">还原</v-btn>
        <v-btn
          color="primary"
          variant="flat"
          :loading="saving"
          :disabled="!dirty || notFound"
          @click="save"
        >
          保存
        </v-btn>
      </v-card-actions>
    </v-card>

    <v-snackbar v-model="snack.show" :color="snack.color" timeout="2800">
      {{ snack.text }}
    </v-snackbar>
  </v-container>
</template>

<style scoped>
.stage-table :deep(td) {
  vertical-align: top;
}
</style>
