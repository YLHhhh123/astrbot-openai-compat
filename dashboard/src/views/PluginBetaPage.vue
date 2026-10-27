<script setup>
/**
 * 插件内测页面。
 *
 * 把插件标记为「内测」后，只有以下用户能使用它：
 *   - 白名单用户 ID
 *   - 兑换过一次性内测码的用户
 *   - 管理员（可开关）
 *
 * 未获授权的用户触发内测插件命令时，内核会拦截并回复这里配置的提示语；
 * 内测插件的被动钩子与 LLM 工具则静默跳过。
 *
 * 插件名支持模糊选中（简写 / 中文都能命中），选中值始终存完整注册名。
 *
 * 后端：/api/v1/plugin-beta/*
 */
import { apiV1Client } from '@/api/http';
import PluginNameInput from '@/components/shared/PluginNameInput.vue';
import { computed, onMounted, ref } from 'vue';

const loading = ref(false);
const saving = ref(false);
const creating = ref(false);

const enabled = ref(false);
const plugins = ref([]);
const userIdsText = ref('');
const adminBypass = ref(true);
const blockMessage = ref('');
const redeemMessage = ref('');

const keys = ref({});
const grants = ref({});
const wildcard = ref('*');

// 生成内测码
const keyCount = ref(1);
const keyScope = ref('*');
const keyNote = ref('');
const keyDays = ref(0);
const newKeys = ref([]);

const checkUid = ref('');
const checkResult = ref(null);

const snack = ref({ show: false, text: '', color: 'success' });
function notify(text, color = 'success') {
  snack.value = { show: true, text: String(text), color };
}

const userIds = computed(() =>
  String(userIdsText.value || '')
    .split(/[,，;；\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
);

const dirty = computed(() => true);

const keyRows = computed(() =>
  Object.entries(keys.value || {})
    .map(([key, item]) => ({ key, ...item }))
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0))
);

const grantRows = computed(() =>
  Object.entries(grants.value || {}).map(([uid, item]) => ({ uid, ...item }))
);

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function keyState(item) {
  if (item.used) return { text: `已用 · ${item.used_by}`, color: 'success' };
  if (item.expired) return { text: '已过期', color: 'grey' };
  return { text: '未使用', color: 'info' };
}

function scopeText(scope) {
  return !scope || scope === wildcard.value ? '全部内测插件' : scope;
}

async function load() {
  loading.value = true;
  try {
    const res = await apiV1Client.get('/plugin-beta/config');
    const d = res?.data?.data || {};
    const cfg = d.config || {};
    enabled.value = !!cfg.enabled;
    plugins.value = [...(cfg.plugins || [])];
    userIdsText.value = (cfg.user_ids || []).join(', ');
    adminBypass.value = cfg.admin_bypass !== false;
    blockMessage.value = cfg.block_message || '';
    redeemMessage.value = cfg.redeem_success_message || '';
    keys.value = d.keys || {};
    grants.value = d.grants || {};
    wildcard.value = d.wildcard || '*';
    if (!keyScope.value || keyScope.value === '') keyScope.value = wildcard.value;
  } catch (error) {
    notify(`加载失败：${error?.response?.data?.message || error?.message || error}`, 'error');
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const payload = {
      enabled: enabled.value,
      plugins: plugins.value,
      user_ids: userIds.value,
      admin_bypass: adminBypass.value,
      block_message: blockMessage.value,
      redeem_success_message: redeemMessage.value
    };
    const res = await apiV1Client.put('/plugin-beta/config', payload);
    const cfg = res?.data?.data?.config || {};
    plugins.value = [...(cfg.plugins || [])];
    notify('已保存并即时生效');
    await load();
  } catch (error) {
    notify(`保存失败：${error?.response?.data?.message || error?.message || error}`, 'error');
  } finally {
    saving.value = false;
  }
}

async function createKeys() {
  creating.value = true;
  try {
    const res = await apiV1Client.post('/plugin-beta/keys', {
      count: Number(keyCount.value) || 1,
      scope: keyScope.value || wildcard.value,
      note: keyNote.value || '',
      expires_in_days: Number(keyDays.value) || 0
    });
    newKeys.value = res?.data?.data?.keys || [];
    notify(`已生成 ${newKeys.value.length} 个内测码`);
    await load();
  } catch (error) {
    notify(`生成失败：${error?.response?.data?.message || error?.message || error}`, 'error');
  } finally {
    creating.value = false;
  }
}

async function deleteKey(key) {
  try {
    await apiV1Client.delete('/plugin-beta/keys', { params: { key } });
    notify('已删除该内测码，其授予的资格已回收');
    await load();
  } catch (error) {
    notify(`删除失败：${error?.response?.data?.message || error?.message || error}`, 'error');
  }
}

async function revokeGrant(uid) {
  try {
    await apiV1Client.delete('/plugin-beta/grants', { params: { uid } });
    notify(`已撤销 ${uid} 的内测资格`);
    await load();
  } catch (error) {
    notify(`撤销失败：${error?.response?.data?.message || error?.message || error}`, 'error');
  }
}

async function doCheck() {
  const uid = String(checkUid.value || '').trim();
  if (!uid) return;
  try {
    const res = await apiV1Client.get('/plugin-beta/check', { params: { uid } });
    checkResult.value = res?.data?.data || null;
  } catch (error) {
    notify(`查询失败：${error?.response?.data?.message || error?.message || error}`, 'error');
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text));
    notify('已复制');
  } catch {
    notify('复制失败，请手动选择复制', 'warning');
  }
}

onMounted(load);
</script>

<template>
  <v-container fluid class="pa-4">
    <v-card class="mb-4">
      <v-card-title class="text-h6 d-flex align-center">
        插件内测
        <v-chip size="small" class="ml-3" variant="tonal" :color="enabled ? 'success' : 'grey'">
          {{ enabled ? '已启用' : '未启用' }}
        </v-chip>
      </v-card-title>
      <v-card-subtitle>
        选中的插件只有白名单用户或兑换过内测码的用户可用，其他人会被拦截并收到自定义提示
      </v-card-subtitle>

      <v-card-text>
        <v-skeleton-loader v-if="loading" type="card" />

        <template v-else>
          <v-switch
            v-model="enabled"
            color="primary"
            label="启用内测准入"
            hint="关闭后所有插件对所有人可用（内测名单保留）"
            persistent-hint
            density="comfortable"
            class="mb-4"
          />

          <div class="text-subtitle-2 mb-2">内测插件</div>
          <PluginNameInput
            v-model="plugins"
            multiple
            label="内测插件"
            placeholder="输入插件名，支持简写 / 中文，如 quote_pool"
            hint="保存时自动解析成完整注册名；插件卸载后名称保留，重新装上即生效"
          />

          <v-divider class="my-5" />

          <div class="text-subtitle-2 mb-2">白名单用户 ID</div>
          <v-text-field
            v-model="userIdsText"
            placeholder="如 2717858671, 1098173324"
            hint="逗号或空格分隔；这些用户无需内测码即可使用全部内测插件"
            persistent-hint
            density="compact"
            variant="outlined"
            class="mb-4"
          />

          <v-switch
            v-model="adminBypass"
            color="primary"
            label="管理员自动放行"
            hint="三级超管（普通超管及以上）无需授权即可使用内测插件"
            persistent-hint
            density="comfortable"
            class="mb-4"
          />

          <div class="text-subtitle-2 mb-2">拦截提示</div>
          <v-textarea
            v-model="blockMessage"
            rows="3"
            placeholder="未获授权的用户触发内测插件命令时收到的提示"
            hint="留空使用默认提示；支持换行"
            persistent-hint
            density="compact"
            variant="outlined"
            class="mb-4"
          />

          <div class="text-subtitle-2 mb-2">兑换成功提示</div>
          <v-textarea
            v-model="redeemMessage"
            rows="2"
            placeholder="用户用 /内测 <内测码> 兑换成功后的提示"
            hint="留空使用默认提示"
            persistent-hint
            density="compact"
            variant="outlined"
          />
        </template>
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn variant="text" :disabled="loading" @click="load">重新加载</v-btn>
        <v-btn color="primary" variant="flat" :loading="saving" :disabled="loading" @click="save">
          保存并生效
        </v-btn>
      </v-card-actions>
    </v-card>

    <v-card class="mb-4">
      <v-card-title class="text-h6">一次性内测码</v-card-title>
      <v-card-subtitle>
        用户发送 <code>/内测 &lt;内测码&gt;</code> 兑换；每个码只能被一个用户使用
      </v-card-subtitle>

      <v-card-text>
        <v-row dense align="center">
          <v-col cols="12" sm="2">
            <v-text-field
              v-model="keyCount"
              label="数量"
              type="number"
              min="1"
              max="50"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
          <v-col cols="12" sm="4">
            <v-select
              v-model="keyScope"
              :items="[
                { title: '全部内测插件', value: wildcard },
                ...plugins.map((name) => ({ title: name, value: name }))
              ]"
              label="适用范围"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
          <v-col cols="12" sm="2">
            <v-text-field
              v-model="keyDays"
              label="有效天数"
              type="number"
              min="0"
              hint="0 = 永久"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
          <v-col cols="12" sm="3">
            <v-text-field
              v-model="keyNote"
              label="备注"
              placeholder="发放对象"
              density="compact"
              variant="outlined"
              hide-details
            />
          </v-col>
          <v-col cols="12" sm="1">
            <v-btn block color="primary" variant="tonal" :loading="creating" @click="createKeys">
              生成
            </v-btn>
          </v-col>
        </v-row>

        <v-alert v-if="newKeys.length" type="success" variant="tonal" class="mt-4">
          <div class="text-subtitle-2 mb-2">新生成的内测码（明文只显示这一次）</div>
          <div class="d-flex flex-wrap ga-2">
            <v-chip
              v-for="k in newKeys"
              :key="k"
              variant="flat"
              color="success"
              @click="copyText(k)"
            >
              {{ k }}
            </v-chip>
          </div>
          <v-btn
            size="small"
            variant="text"
            class="mt-2"
            prepend-icon="mdi-content-copy"
            @click="copyText(newKeys.join('\n'))"
          >
            全部复制
          </v-btn>
        </v-alert>

        <v-table v-if="keyRows.length" density="compact" class="mt-4">
          <thead>
            <tr>
              <th>内测码</th>
              <th style="width: 160px">范围</th>
              <th style="width: 130px">状态</th>
              <th style="width: 140px">备注</th>
              <th style="width: 150px">生成时间</th>
              <th style="width: 80px"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in keyRows" :key="row.key">
              <td><code>{{ row.key }}</code></td>
              <td class="text-caption">{{ scopeText(row.scope) }}</td>
              <td>
                <v-chip size="x-small" variant="tonal" :color="keyState(row).color">
                  {{ keyState(row).text }}
                </v-chip>
              </td>
              <td class="text-caption">{{ row.note || '—' }}</td>
              <td class="text-caption">{{ fmtTime(row.created_at) }}</td>
              <td>
                <v-btn size="x-small" variant="text" color="error" @click="deleteKey(row.key)">
                  删除
                </v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>
        <div v-else class="text-caption text-medium-emphasis mt-4">还没有生成内测码</div>
      </v-card-text>
    </v-card>

    <v-card class="mb-4">
      <v-card-title class="text-h6">已兑换用户</v-card-title>
      <v-card-subtitle>通过内测码获得资格的用户（白名单用户不在此列）</v-card-subtitle>
      <v-card-text>
        <v-table v-if="grantRows.length" density="compact">
          <thead>
            <tr>
              <th style="width: 160px">用户 ID</th>
              <th>可用插件</th>
              <th style="width: 180px">来源内测码</th>
              <th style="width: 150px">兑换时间</th>
              <th style="width: 80px"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in grantRows" :key="row.uid">
              <td>{{ row.uid }}</td>
              <td class="text-caption">
                <v-chip
                  v-for="p in row.plugins || []"
                  :key="p"
                  size="x-small"
                  variant="tonal"
                  class="mr-1"
                >
                  {{ scopeText(p) }}
                </v-chip>
              </td>
              <td class="text-caption"><code>{{ row.source || '—' }}</code></td>
              <td class="text-caption">{{ fmtTime(row.at) }}</td>
              <td>
                <v-btn size="x-small" variant="text" color="error" @click="revokeGrant(row.uid)">
                  撤销
                </v-btn>
              </td>
            </tr>
          </tbody>
        </v-table>
        <div v-else class="text-caption text-medium-emphasis">还没有用户兑换内测码</div>

        <v-divider class="my-5" />

        <div class="text-subtitle-2 mb-2">查询某个用户的内测状态</div>
        <v-row dense align="center">
          <v-col cols="12" sm="6" md="4">
            <v-text-field
              v-model="checkUid"
              label="用户 ID"
              density="compact"
              variant="outlined"
              hide-details
              @keyup.enter="doCheck"
            />
          </v-col>
          <v-col cols="12" sm="3" md="2">
            <v-btn block color="primary" variant="tonal" :disabled="!checkUid" @click="doCheck">
              查询
            </v-btn>
          </v-col>
          <v-col v-if="checkResult" cols="12" md="6">
            <v-alert type="info" variant="tonal" density="compact" class="mb-0">
              {{ checkResult.uid }} →
              <strong v-if="checkResult.allowed_all">全部内测插件</strong>
              <strong v-else-if="checkResult.whitelisted">白名单用户</strong>
              <strong v-else-if="(checkResult.granted || []).length">
                {{ checkResult.granted.join('、') }}
              </strong>
              <strong v-else>无内测资格</strong>
            </v-alert>
          </v-col>
        </v-row>
      </v-card-text>
    </v-card>

    <v-snackbar v-model="snack.show" :color="snack.color" timeout="2800">
      {{ snack.text }}
    </v-snackbar>
  </v-container>
</template>
