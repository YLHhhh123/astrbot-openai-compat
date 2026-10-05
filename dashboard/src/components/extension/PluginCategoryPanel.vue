<script setup>
/**
 * 插件分类与入口策略面板。
 *
 * - 插件按能力性质分为「功能性」与「互动性」，默认互动性（保守）。
 * - 各入口可配置允许的类别；受限入口（个人微信 openclaw、OpenAI 兼容 HTTP 接口）
 *   默认只放行功能性插件。
 */
import { apiV1Client } from "@/api/http";
import { computed, onMounted, ref } from "vue";

const loading = ref(false);
const savingPolicy = ref(false);
const expanded = ref([]);
const plugins = ref([]);
const categories = ref([]);
const policy = ref({});
const defaultPolicy = ref({});

const snack = ref({ show: false, text: "", color: "success" });

function notify(text, color = "success") {
  snack.value = { show: true, text: String(text), color };
}

const unclassifiedCount = computed(
  () => plugins.value.filter((item) => item.source === "default" && !item.reserved).length,
);

const entries = computed(() => {
  const keys = new Set([
    ...Object.keys(defaultPolicy.value || {}),
    ...Object.keys(policy.value || {}),
  ]);
  return Array.from(keys).sort();
});

function categoryLabel(value) {
  return categories.value.find((item) => item.value === value)?.label || value;
}

function defaultLabel(entry) {
  const values = defaultPolicy.value?.[entry];
  if (!values) return "跟随 default";
  return values.map(categoryLabel).join(" / ");
}

async function load() {
  loading.value = true;
  try {
    const [metaRes, pluginsRes] = await Promise.all([
      apiV1Client.get("/plugin-categories/meta"),
      apiV1Client.get("/plugin-categories/plugins"),
    ]);
    const meta = metaRes?.data?.data || {};
    categories.value = meta.categories || [];
    policy.value = { ...(meta.entry_policy || {}) };
    defaultPolicy.value = meta.default_entry_policy || {};
    plugins.value = pluginsRes?.data?.data || [];
  } catch (error) {
    notify(`加载失败：${error?.message || error}`, "error");
  } finally {
    loading.value = false;
  }
}

async function updatePluginCategory(item, value) {
  if (!value || value === item.category) return;
  try {
    await apiV1Client.patch(
      `/plugin-categories/plugins/${encodeURIComponent(item.name)}`,
      { category: value },
    );
    item.category = value;
    item.source = "override";
    item.category_label = categoryLabel(value);
    notify(`已把 ${item.display_name || item.name} 设为「${categoryLabel(value)}」`);
  } catch (error) {
    notify(`保存失败：${error?.message || error}`, "error");
  }
}

async function resetPluginCategory(item) {
  try {
    await apiV1Client.delete(
      `/plugin-categories/plugins/${encodeURIComponent(item.name)}`,
    );
    notify(`已清除 ${item.display_name || item.name} 的覆盖`);
    await load();
  } catch (error) {
    notify(`清除失败：${error?.message || error}`, "error");
  }
}

async function savePolicy() {
  savingPolicy.value = true;
  try {
    const payload = {};
    entries.value.forEach((entry) => {
      payload[entry] = policy.value[entry] || [];
    });
    const res = await apiV1Client.put("/plugin-categories/entry-policy", {
      policy: payload,
    });
    policy.value = { ...(res?.data?.data?.policy || {}) };
    notify("入口策略已保存");
  } catch (error) {
    notify(`保存失败：${error?.message || error}`, "error");
  } finally {
    savingPolicy.value = false;
  }
}

onMounted(load);
</script>

<template>
  <v-expansion-panels v-model="expanded" variant="accordion" class="mb-4">
    <v-expansion-panel value="categories">
      <v-expansion-panel-title>
        <div class="d-flex align-center ga-3 flex-wrap">
          <v-icon icon="mdi-shape-outline" size="20" />
          <span class="font-weight-medium">插件分类与入口策略</span>
          <v-chip size="x-small" color="info" variant="tonal">
            功能性 {{ plugins.filter((p) => p.category === "functional").length }} ·
            互动性 {{ plugins.filter((p) => p.category === "interactive").length }}
          </v-chip>
          <v-chip
            v-if="unclassifiedCount"
            size="x-small"
            color="warning"
            variant="tonal"
          >
            未显式分类 {{ unclassifiedCount }}
          </v-chip>
        </div>
      </v-expansion-panel-title>

      <v-expansion-panel-text>
        <div class="text-caption text-medium-emphasis mb-3">
          功能性插件依赖 LLM 工具能力，所有入口可用；互动性插件依赖平台社交能力
          （收发消息、@、点赞等），在受限入口（个人微信 openclaw、OpenAI 兼容 HTTP
          接口）不参与运行。未标注的插件按<strong>互动性</strong>处理。
        </div>

        <v-alert
          v-if="unclassifiedCount"
          type="warning"
          variant="tonal"
          density="compact"
          class="mb-3"
        >
          有 {{ unclassifiedCount }} 个插件既未在元数据声明类别、也未手动设置，正在按默认
          「互动性」处理。若它们是功能性插件（提供工具能力），请在下方显式设置。
        </v-alert>

        <h4 class="text-subtitle-2 mb-2">各入口允许的类别</h4>
        <v-table density="compact" class="mb-3">
          <thead>
            <tr>
              <th style="width: 30%">入口</th>
              <th>允许的类别</th>
              <th style="width: 22%">默认值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in entries" :key="entry">
              <td>
                <code>{{ entry }}</code>
              </td>
              <td>
                <v-select
                  v-model="policy[entry]"
                  :items="categories"
                  item-title="label"
                  item-value="value"
                  multiple
                  chips
                  density="compact"
                  variant="outlined"
                  hide-details
                />
              </td>
              <td class="text-caption text-medium-emphasis">
                {{ defaultLabel(entry) }}
              </td>
            </tr>
          </tbody>
        </v-table>

        <div class="d-flex justify-end mb-5">
          <v-btn
            color="primary"
            size="small"
            :loading="savingPolicy"
            @click="savePolicy"
          >
            保存入口策略
          </v-btn>
        </div>

        <h4 class="text-subtitle-2 mb-2">插件类别</h4>
        <v-table density="compact">
          <thead>
            <tr>
              <th>插件</th>
              <th style="width: 26%">类别</th>
              <th style="width: 12%">来源</th>
              <th style="width: 8%"></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in plugins" :key="item.name">
              <td>
                <div class="d-flex align-center ga-2">
                  <span>{{ item.display_name || item.name }}</span>
                  <v-chip v-if="item.reserved" size="x-small" variant="text">
                    系统
                  </v-chip>
                </div>
                <div class="text-caption text-medium-emphasis">{{ item.name }}</div>
              </td>
              <td>
                <v-select
                  :model-value="item.category"
                  :items="categories"
                  item-title="label"
                  item-value="value"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :disabled="item.reserved"
                  @update:model-value="(value) => updatePluginCategory(item, value)"
                />
              </td>
              <td>
                <v-chip
                  size="x-small"
                  variant="tonal"
                  :color="
                    item.source === 'override'
                      ? 'primary'
                      : item.source === 'metadata'
                        ? 'success'
                        : 'warning'
                  "
                >
                  {{ item.source }}
                </v-chip>
              </td>
              <td>
                <v-btn
                  v-if="item.source === 'override'"
                  icon="mdi-restore"
                  size="x-small"
                  variant="text"
                  title="清除覆盖"
                  @click="resetPluginCategory(item)"
                />
              </td>
            </tr>
          </tbody>
        </v-table>

        <div class="d-flex justify-end mt-3">
          <v-btn size="small" variant="text" :loading="loading" @click="load">
            刷新
          </v-btn>
        </div>
      </v-expansion-panel-text>
    </v-expansion-panel>
  </v-expansion-panels>

  <v-snackbar v-model="snack.show" :color="snack.color" timeout="3000">
    {{ snack.text }}
  </v-snackbar>
</template>
