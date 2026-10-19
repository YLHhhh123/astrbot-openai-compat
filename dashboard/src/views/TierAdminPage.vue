<script setup>
/**
 * 管理员等级页面。
 *
 * 编辑内置插件 builtin_commands 的三级超管配置：
 *   - 最高 / 中级 / 普通超管的 ID 名单
 *   - 每个层级可用哪些指令（留空=全部，"禁用"=全部禁止）
 *   - 每个层级禁止使用哪些插件
 *
 * 也可查询任意用户 ID 当前被判定的层级。
 */
import { apiV1Client } from '@/api/http';
import { computed, onMounted, ref } from 'vue';

const loading = ref(false);
const saving = ref(false);
const tierLevels = ref([]);
const idLists = ref({ top_admin_ids: [], mid_admin_ids: [], normal_admin_ids: [] });
const commands = ref({});
const forbidden = ref({});
const snack = ref({ show: false, text: '', color: 'success' });

// 编辑用文本（逗号分隔）
const idText = ref({ top_admin_ids: '', mid_admin_ids: '', normal_admin_ids: '' });
const commandText = ref({});
const forbiddenText = ref({});

const resolveId = ref('');
const resolveResult = ref(null);

const idKeyOf = { top_admin: 'top_admin_ids', mid_admin: 'mid_admin_ids', normal_admin: 'normal_admin_ids' };
const superRoles = ['top_admin', 'mid_admin', 'normal_admin'];

const superLevels = computed(() =>
  superRoles.map((role) => tierLevels.value.find((t) => t.value === role)).filter(Boolean)
);

const commandHint = '留空=全部可用；填「禁用」=全部不可用；否则逗号分隔（子命令写作 “plugin ls”）';

function notify(text, color = 'success') {
  snack.value = { show: true, text: String(text), color };
}

function fillText() {
  for (const role of superRoles) {
    const key = idKeyOf[role];
    idText.value[key] = (idLists.value[key] || []).join(', ');
  }
  commandText.value = { ...commands.value };
  forbiddenText.value = {};
  for (const [role, list] of Object.entries(forbidden.value || {})) {
    forbiddenText.value[role] = (list || []).join(', ');
  }
}

async function load() {
  loading.value = true;
  try {
    const res = await apiV1Client.get('/tier-admin/config');
    const d = res?.data?.data || {};
    tierLevels.value = d.tier_levels || [];
    idLists.value = d.id_lists || {};
    commands.value = d.commands || {};
    forbidden.value = d.forbidden_plugins || {};
    fillText();
  } catch (error) {
    notify(`加载失败：${error?.response?.data?.message || error}`, 'error');
  } finally {
    loading.value = false;
  }
}

async function save() {
  saving.value = true;
  try {
    const payload = {
      commands: { ...commandText.value },
      forbidden_plugins: { ...forbiddenText.value }
    };
    for (const role of superRoles) {
      const key = idKeyOf[role];
      payload[key] = idText.value[key] || '';
    }
    const res = await apiV1Client.put('/tier-admin/config', payload);
    const msg = res?.data?.message || '已保存';
    if (msg.includes('无变化')) {
      notify('配置无变化');
    } else if (msg.includes('落盘失败')) {
      notify(msg, 'error');
    } else {
      notify('已保存并同步到运行时');
      await load();
    }
  } catch (error) {
    notify(`保存失败：${error?.response?.data?.message || error}`, 'error');
  } finally {
    saving.value = false;
  }
}

async function doResolve() {
  const uid = resolveId.value.trim();
  if (!uid) return;
  try {
    const res = await apiV1Client.get('/tier-admin/resolve', { params: { user_id: uid } });
    const d = res?.data?.data || {};
    const labels = (d.roles || []).map((r) => tierLevels.value.find((t) => t.value === r)?.label || r);
    resolveResult.value = { uid, labels };
  } catch (error) {
    notify(`查询失败：${error?.response?.data?.message || error}`, 'error');
  }
}
</script>

<template>
  <v-container fluid class="pa-4">
    <v-card>
      <v-card-title class="text-h6">管理员等级</v-card-title>
      <v-card-subtitle>三级超管与指令权限（作用于内置指令与会话指令集）</v-card-subtitle>
      <v-card-text>
        <v-alert type="info" variant="tonal" density="compact" class="mb-4">
          层级从高到低：最高超管 &gt; 中级超管 &gt; 普通超管 &gt; 群主 &gt; 群管理员 &gt; 普通成员。
          未列入任何名单的用户按普通成员处理。修改后立即生效并落盘。
        </v-alert>

        <v-skeleton-loader v-if="loading" type="card" />
        <template v-else>
          <v-row>
            <v-col v-for="lv in superLevels" :key="lv.value" cols="12" md="4">
              <v-card variant="outlined" class="h-100">
                <v-card-title class="text-subtitle-1">
                  {{ lv.label }}
                  <v-chip size="x-small" class="ml-2" variant="tonal">rank {{ lv.rank }}</v-chip>
                </v-card-title>
                <v-card-text>
                  <v-textarea
                    v-model="idText[idKeyOf[lv.value]]"
                    :label="`${lv.label} ID 名单`"
                    :hint="lv.value === 'top_admin' ? '建议把自己填在这里，只有最高超管能 /setop' : '留空表示该层级无人'"
                    persistent-hint
                    rows="3"
                    auto-grow
                    density="compact"
                    variant="outlined"
                  />
                </v-card-text>
              </v-card>
            </v-col>
          </v-row>

          <v-divider class="my-5" />

          <div class="text-subtitle-2 mb-2">各层级可用指令 / 禁止插件</div>
          <v-table density="compact">
            <thead>
              <tr>
                <th style="width: 140px">层级</th>
                <th>可用指令</th>
                <th style="width: 260px">禁止使用的插件</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="lv in tierLevels" :key="lv.value">
                <td>
                  {{ lv.label }}
                  <div class="text-caption text-medium-emphasis">rank {{ lv.rank }}</div>
                </td>
                <td>
                  <v-text-field
                    v-model="commandText[lv.value]"
                    :placeholder="lv.value === 'top_admin' ? '留空 = 全部可用' : '留空 = 全部可用'"
                    :hint="commandHint"
                    persistent-hint
                    density="compact"
                    variant="outlined"
                  />
                </td>
                <td>
                  <v-text-field
                    v-model="forbiddenText[lv.value]"
                    placeholder="留空 = 不禁止"
                    hint="插件名，逗号分隔"
                    persistent-hint
                    density="compact"
                    variant="outlined"
                  />
                </td>
              </tr>
            </tbody>
          </v-table>

          <v-divider class="my-5" />

          <div class="text-subtitle-2 mb-2">查询用户层级</div>
          <v-row dense align="center">
            <v-col cols="12" sm="8" md="5">
              <v-text-field
                v-model="resolveId"
                label="用户 ID"
                hint="支持纯数字，或 @名字(数字) 形式"
                persistent-hint
                density="compact"
                variant="outlined"
                @keyup.enter="doResolve"
              />
            </v-col>
            <v-col cols="12" sm="4" md="3">
              <v-btn block color="primary" variant="tonal" :disabled="!resolveId" @click="doResolve">
                查询
              </v-btn>
            </v-col>
            <v-col v-if="resolveResult" cols="12" md="4">
              <v-alert type="info" variant="tonal" density="compact" class="mb-0">
                {{ resolveResult.uid }} →
                <strong>{{ resolveResult.labels.join(' / ') || '未知' }}</strong>
              </v-alert>
            </v-col>
          </v-row>
        </template>
      </v-card-text>
      <v-card-actions>
        <v-spacer />
        <v-btn color="primary" variant="flat" :loading="saving" :disabled="loading" @click="save">
          保存
        </v-btn>
      </v-card-actions>
    </v-card>

    <v-snackbar v-model="snack.show" :color="snack.color" timeout="2600">
      {{ snack.text }}
    </v-snackbar>
  </v-container>
</template>
