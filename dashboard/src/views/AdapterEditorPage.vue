<script setup>
/**
 * 适配器编辑面板。
 *
 * 三层防护的可视化：
 *   1. 总开关（开启需审批等级，防止普通 admin 悄悄打开）
 *   2. 不可修改的适配器清单
 *   3. 逐条提案的 diff 审阅 + 批准/驳回 + 应用/回退
 *
 * 后端：/api/v1/adapter-editor/*
 */
import { apiV1Client } from '@/api/http';
import { computed, onMounted, ref } from 'vue';

const loading = ref(false);
const busy = ref(false);
const status = ref(null);
const proposals = ref([]);
const auditLog = ref([]);
const filterStatus = ref('pending');
const detail = ref(null);        // 当前查看的提案（含 diff）
const protectedDraft = ref([]);
const snack = ref({ show: false, text: '', color: 'success' });

const STATUS_LABEL = {
  pending: '待审',
  approved: '已批准待应用',
  applied: '已应用',
  rejected: '已驳回',
  failed: '失败',
};

function notify(text, color = 'success') {
  snack.value = { show: true, text: String(text), color };
}

const counts = computed(() => status.value?.counts || {});

async function loadStatus() {
  status.value = (await apiV1Client.get('/adapter-editor/status'))?.data?.data || null;
  protectedDraft.value = [...(status.value?.protected_adapters || [])];
}

async function loadProposals() {
  const res = await apiV1Client.get('/adapter-editor/proposals', {
    params: { status: filterStatus.value },
  });
  proposals.value = res?.data?.data || [];
}

async function loadAudit() {
  const res = await apiV1Client.get('/adapter-editor/audit', { params: { lines: 60 } });
  auditLog.value = res?.data?.data || [];
}

async function reload() {
  loading.value = true;
  try {
    await Promise.all([loadStatus(), loadProposals(), loadAudit()]);
  } catch (error) {
    notify(`加载失败：${error?.response?.data?.data?.error || error?.message || error}`, 'error');
  } finally {
    loading.value = false;
  }
}

async function toggleSwitch(value) {
  busy.value = true;
  try {
    await apiV1Client.put('/adapter-editor/switch', { enabled: value });
    notify(value ? '已开启：AI 现在可以提交提案（仍需逐条审批）' : '已关闭');
    await reload();
  } catch (error) {
    notify(`操作失败：${error?.response?.data?.data?.error || error?.message || error}`, 'error');
    await loadStatus(); // 回滚 UI 显示
  } finally {
    busy.value = false;
  }
}

async function saveProtected() {
  busy.value = true;
  try {
    await apiV1Client.put('/adapter-editor/protected', { adapters: protectedDraft.value });
    notify('不可修改清单已保存');
    await loadStatus();
  } catch (error) {
    notify(`保存失败：${error?.response?.data?.data?.error || error?.message || error}`, 'error');
  } finally {
    busy.value = false;
  }
}

async function openDetail(pid) {
  try {
    detail.value = (await apiV1Client.get(`/adapter-editor/proposals/${pid}`))?.data?.data || null;
  } catch (error) {
    notify(`读取提案失败：${error?.message || error}`, 'error');
  }
}

async function act(pid, action, note = '') {
  busy.value = true;
  try {
    const res = await apiV1Client.post(`/adapter-editor/proposals/${pid}/${action}`, { note });
    const d = res?.data?.data || {};
    if (d.need_restart) {
      notify('已写入文件。**需重启 AstrBot 才生效**，请在服务管理里重启');
    } else {
      notify(d.message || '操作完成');
    }
    detail.value = null;
    await reload();
  } catch (error) {
    notify(`操作失败：${error?.response?.data?.data?.error || error?.message || error}`, 'error');
  } finally {
    busy.value = false;
  }
}

function ts(v) {
  return v ? new Date(v * 1000).toLocaleString() : '';
}

onMounted(reload);
</script>

<template>
  <v-container fluid class="pa-4">
    <!-- 开关 -->
    <v-card class="mb-4">
      <v-card-title class="text-h6 d-flex align-center">
        适配器编辑
        <v-chip v-if="status && status.plugin_loaded === false" size="x-small" class="ml-3" color="warning">
          插件未加载
        </v-chip>
      </v-card-title>
      <v-card-subtitle>AI 可提交平台适配器改动，管理员逐条审批后才落盘</v-card-subtitle>
      <v-card-text>
        <v-alert type="warning" variant="tonal" density="compact" class="mb-4">
          AI 只能调用提案工具，拿不到 shell / 文件写入能力。适配器的<strong>出站发消息逻辑</strong>、
          白名单管道、provider、数据库等核心路径被硬性禁止修改。
        </v-alert>

        <v-switch
          :model-value="status?.enabled"
          :disabled="busy || loading"
          color="error"
          hide-details
          @update:model-value="toggleSwitch"
        />
        <div class="text-body-2 mt-1">
          <strong>{{ status?.enabled ? '已开启' : '已关闭' }}</strong>
          —— {{ status?.enabled
            ? 'AI 可以提交提案，但仍需你在下方逐条批准'
            : 'AI 的适配器编辑工具当前不可用' }}
        </div>

        <v-row class="mt-4" dense>
          <v-col cols="12" md="3">
            <v-card variant="outlined" class="pa-3">
              <div class="text-caption text-medium-emphasis">审批权限</div>
              <div class="text-subtitle-1">{{ status?.approve_tier || 'top_admin' }}</div>
            </v-card>
          </v-col>
          <v-col cols="6" md="3">
            <v-card variant="outlined" class="pa-3">
              <div class="text-caption text-medium-emphasis">待审</div>
              <div class="text-h6 text-warning">{{ counts.pending ?? 0 }}</div>
            </v-card>
          </v-col>
          <v-col cols="6" md="3">
            <v-card variant="outlined" class="pa-3">
              <div class="text-caption text-medium-emphasis">待应用</div>
              <div class="text-h6 text-info">{{ counts.approved ?? 0 }}</div>
            </v-card>
          </v-col>
          <v-col cols="6" md="3">
            <v-card variant="outlined" class="pa-3">
              <div class="text-caption text-medium-emphasis">已应用</div>
              <div class="text-h6 text-success">{{ counts.applied ?? 0 }}</div>
            </v-card>
          </v-col>
        </v-row>

        <v-expansion-panels variant="accordion" class="mt-4">
          <v-expansion-panel title="不可修改的适配器清单">
            <v-card-text>
              <v-textarea
                v-model="protectedDraft"
                label="逗号分隔的适配器名"
                hint="这些适配器拒绝任何改动（含 AI 试图新建同名适配器）。默认已含 aiocqhttp"
                persistent-hint
                rows="2"
                auto-grow
                density="compact"
                variant="outlined"
              />
              <v-btn class="mt-3" color="primary" variant="tonal" :loading="busy" @click="saveProtected">
                保存清单
              </v-btn>
            </v-card-text>
          </v-expansion-panel>
          <v-expansion-panel title="配额与限制">
            <v-card-text>
              <v-row dense>
                <v-col cols="6" md="3">
                  <v-card variant="outlined" class="pa-3">
                    <div class="text-caption">每会话提案上限</div>
                    <div class="text-h6">{{ status?.quota?.per_session }}</div>
                  </v-card>
                </v-col>
                <v-col cols="6" md="3">
                  <v-card variant="outlined" class="pa-3">
                    <div class="text-caption">每日上限</div>
                    <div class="text-h6">{{ status?.quota?.per_day }}</div>
                  </v-card>
                </v-col>
                <v-col cols="6" md="3">
                  <v-card variant="outlined" class="pa-3">
                    <div class="text-caption">单次改动行数上限</div>
                    <div class="text-h6">{{ status?.quota?.max_diff_lines }}</div>
                  </v-card>
                </v-col>
                <v-col cols="6" md="3">
                  <v-card variant="outlined" class="pa-3">
                    <div class="text-caption">允许新建适配器</div>
                    <div class="text-h6">{{ status?.allow_new_adapter ? '是' : '否' }}</div>
                  </v-card>
                </v-col>
              </v-row>
            </v-card-text>
          </v-expansion-panel>
        </v-expansion-panels>
      </v-card-text>
    </v-card>

    <!-- 提案列表 -->
    <v-card class="mb-4">
      <v-card-title class="text-subtitle-1">
        改动提案
        <v-btn icon="mdi-refresh" size="small" variant="text" class="ml-2" @click="reload" />
      </v-card-title>
      <v-card-text>
        <v-btn-toggle v-model="filterStatus" mandatory density="compact" class="mb-3">
          <v-btn v-for="(label, key) in STATUS_LABEL" :key="key" :value="key" size="small">
            {{ label }}<span v-if="counts[key] != null"> ({{ counts[key] }})</span>
          </v-btn>
        </v-btn-toggle>

        <v-alert v-if="filterStatus === 'approved'" type="info" variant="tonal" density="compact" class="mb-3">
          已批准的提案<strong>尚未写入文件</strong>。点「写入文件」后需要重启 AstrBot 才生效。
        </v-alert>

        <v-skeleton-loader v-if="loading" type="list-item-three-line" />
        <v-list v-else-if="proposals.length" lines="two">
          <v-list-item v-for="p in proposals" :key="p.id">
            <v-list-item-title>
              <code>{{ p.id }}</code>
              <v-chip size="x-small" class="ml-2" variant="tonal">
                {{ p.is_new_file ? '新建' : '修改' }}
              </v-chip>
              <v-chip size="x-small" class="ml-1" variant="tonal">{{ p.adapter }}</v-chip>
            </v-list-item-title>
            <v-list-item-subtitle>
              {{ p.rel_path }} ｜ +{{ p.diff_lines }} 行 ｜ {{ p.author_name }} ｜ {{ ts(p.created_at) }}
            </v-list-item-subtitle>
            <v-list-item-subtitle v-if="p.reason">
              <em>{{ p.reason }}</em>
            </v-list-item-subtitle>
            <v-list-item-subtitle v-if="p.error" class="text-error">
              {{ p.error }}
            </v-list-item-subtitle>
            <template #append>
              <div class="d-flex ga-1">
                <v-btn size="small" variant="text" @click="openDetail(p.id)">diff</v-btn>
                <v-btn
                  v-if="p.status === 'pending'"
                  size="small" color="success" variant="tonal"
                  :loading="busy" @click="act(p.id, 'approve')"
                >
                  批准
                </v-btn>
                <v-btn
                  v-if="p.status === 'pending'"
                  size="small" color="error" variant="text"
                  :loading="busy" @click="act(p.id, 'reject')"
                >
                  驳回
                </v-btn>
                <v-btn
                  v-if="p.status === 'approved'"
                  size="small" color="primary" variant="tonal"
                  :loading="busy" @click="act(p.id, 'apply')"
                >
                  写入文件
                </v-btn>
                <v-btn
                  v-if="p.status === 'applied'"
                  size="small" color="warning" variant="text"
                  :loading="busy" @click="act(p.id, 'rollback')"
                >
                  回退
                </v-btn>
              </div>
            </template>
          </v-list-item>
        </v-list>
        <v-alert v-else type="info" variant="tonal" density="compact">
          没有状态为「{{ STATUS_LABEL[filterStatus] }}」的提案。
        </v-alert>
      </v-card-text>
    </v-card>

    <!-- diff 详情 -->
    <v-dialog v-model="detail" max-width="1000" scrollable>
      <v-card v-if="detail">
        <v-card-title class="text-subtitle-1">
          <code>{{ detail.id }}</code>
          <v-chip size="x-small" class="ml-2">{{ STATUS_LABEL[detail.status] }}</v-chip>
        </v-card-title>
        <v-card-subtitle>{{ detail.rel_path }}</v-card-subtitle>
        <v-card-text>
          <v-alert v-if="detail.reason" type="info" variant="tonal" density="compact" class="mb-3">
            AI 给出的理由：{{ detail.reason }}
          </v-alert>
          <pre class="diff-view">{{ detail.diff || '（无差异，可能是新建空文件）' }}</pre>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="detail = null">关闭</v-btn>
          <v-btn
            v-if="detail.status === 'pending'"
            color="error" variant="text" :loading="busy"
            @click="act(detail.id, 'reject')"
          >
            驳回
          </v-btn>
          <v-btn
            v-if="detail.status === 'pending'"
            color="success" variant="tonal" :loading="busy"
            @click="act(detail.id, 'approve')"
          >
            批准
          </v-btn>
          <v-btn
            v-if="detail.status === 'approved'"
            color="primary" variant="flat" :loading="busy"
            @click="act(detail.id, 'apply')"
          >
            写入文件
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- 审计 -->
    <v-card>
      <v-card-title class="text-subtitle-1">审计日志</v-card-title>
      <v-card-subtitle>所有开关变更、提案、审批与应用动作</v-card-subtitle>
      <v-card-text>
        <v-table density="compact">
          <thead>
            <tr>
              <th style="width: 160px">时间</th>
              <th style="width: 140px">动作</th>
              <th>详情</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(a, i) in auditLog" :key="i">
              <td class="text-caption">{{ a.time }}</td>
              <td><code>{{ a.action }}</code></td>
              <td class="text-caption">
                <span v-for="(v, k) in a" :key="k" v-show="!['ts', 'action', 'time'].includes(k)">
                  {{ k }}={{ typeof v === 'object' ? JSON.stringify(v) : v }}
                </span>
              </td>
            </tr>
          </tbody>
        </v-table>
      </v-card-text>
    </v-card>

    <v-snackbar v-model="snack.show" :color="snack.color" timeout="4000">
      {{ snack.text }}
    </v-snackbar>
  </v-container>
</template>

<style scoped>
.diff-view {
  background: rgba(127, 127, 127, 0.08);
  border: 1px solid rgba(127, 127, 127, 0.3);
  border-radius: 4px;
  padding: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  overflow-x: auto;
  white-space: pre;
  max-height: 60vh;
}
</style>
