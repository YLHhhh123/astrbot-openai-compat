<script setup>
/**
 * 插件分类面板。
 *
 * - 插件按能力性质分为「功能性」与「互动性」，默认功能性（保守反向）。
 * - 各机器人渠道允许的插件类别已移至「机器人设置」（平台适配器页，
 *   每个机器人单独配置），本面板只负责给插件本身归类。
 */
import { apiV1Client } from "@/api/http";
import { computed, onMounted, ref } from "vue";

const loading = ref(false);
const expanded = ref([]);
const plugins = ref([]);
const categories = ref([]);

const snack = ref({ show: false, text: "", color: "success" });

function notify(text, color = "success") {
  snack.value = { show: true, text: String(text), color };
}

const unclassifiedCount = computed(
  () => plugins.value.filter((item) => item.source === "default" && !item.reserved).length,
);

function categoryLabel(value) {
  return categories.value.find((item) => item.value === value)?.label || value;
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

onMounted(load);
</script>

<template>
  <v-expansion-panels v-model="expanded" variant="accordion" class="mb-4">
    <v-expansion-panel value="categories">
      <v-expansion-panel-title>
        <div class="d-flex align-center ga-3 flex-wrap">
          <v-icon icon="mdi-shape-outline" size="20" />
          <span class="font-weight-medium">插件分类</span>
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
          功能性插件依赖 LLM 工具能力，所有渠道可用；互动性插件依赖平台社交能力
          （收发消息、@、点赞等），在受限渠道不参与运行。未标注的插件按默认类别处理。
          各渠道允许的插件类别请在<strong>「机器人设置」</strong>里对每个机器人单独配置。
        </div>

        <v-alert
          v-if="unclassifiedCount"
          type="warning"
          variant="tonal"
          density="compact"
          class="mb-3"
        >
          有 {{ unclassifiedCount }} 个插件既未在元数据声明类别、也未手动设置，
          正在按默认「功能性」处理。若它们是互动性插件（依赖平台社交能力），
          请在下方显式设置。
        </v-alert>

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
