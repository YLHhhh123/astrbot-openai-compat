<template>
  <div class="plugin-name-input">
    <v-combobox
      :model-value="modelValue"
      :items="options"
      :multiple="multiple"
      :chips="multiple"
      :closable-chips="multiple"
      :label="label"
      :placeholder="placeholder"
      :hint="hint"
      :persistent-hint="Boolean(hint)"
      :loading="loading"
      :error-messages="errorMessage ? [errorMessage] : []"
      item-title="title"
      item-value="value"
      variant="outlined"
      density="compact"
      hide-details="auto"
      auto-select-first
      clearable
      :return-object="false"
      @update:search="onSearch"
      @update:model-value="onChange"
    >
      <template #item="{ props: itemProps, item }">
        <v-list-item v-bind="itemProps" :subtitle="item.raw.desc">
          <template #append>
            <v-chip
              v-if="item.raw.match && item.raw.match !== 'exact'"
              size="x-small"
              variant="tonal"
              color="primary"
            >
              {{ matchLabel(item.raw.match) }}
            </v-chip>
          </template>
        </v-list-item>
      </template>

      <template #no-data>
        <v-list-item>
          <v-list-item-title class="text-medium-emphasis">
            {{ searchText ? '没有匹配的插件' : '正在读取插件列表…' }}
          </v-list-item-title>
        </v-list-item>
      </template>

      <template #append-inner>
        <v-progress-circular v-if="loading" indeterminate size="16" width="2" />
      </template>
    </v-combobox>

    <div v-if="multiple && unresolved.length > 0" class="text-caption text-warning mt-1">
      以下插件未能识别：{{ unresolved.join('、') }}
    </div>
  </div>
</template>

<script setup>
/**
 * 插件名输入框（支持模糊选中）。
 *
 * 手打简写即可：`quote_pool` / `quotepool` / `引用池` 都会解析到
 * `astrbot_plugin_quote_pool`。候选来自内核 star_registry，插件增删改后
 * 每次搜索都重新拉取，因此**自动更新**、无需刷新页面。
 *
 * 已选中的值始终是**完整注册名**，所以落库的数据永远是可直接匹配的形式。
 */
import { apiV1Client } from '@/api/http';
import { computed, onMounted, ref } from 'vue';

const props = defineProps({
  modelValue: {
    type: [String, Array],
    default: '',
  },
  multiple: {
    type: Boolean,
    default: false,
  },
  label: {
    type: String,
    default: '插件',
  },
  placeholder: {
    type: String,
    default: '输入插件名，支持简写 / 中文',
  },
  hint: {
    type: String,
    default: '',
  },
  /** 已选但解析失败的原始输入（只读展示用） */
  errorMessage: {
    type: String,
    default: '',
  },
});

const emit = defineEmits(['update:modelValue']);

const loading = ref(false);
const searchText = ref('');
const allPlugins = ref([]);

const MATCH_LABELS = {
  exact: '精确',
  prefix: '前缀',
  substring: '包含',
  tokens: '分词',
  subsequence: '缩写',
  fuzzy: '拼写容错',
  description: '描述',
  'description-tokens': '描述',
};

function matchLabel(match) {
  return MATCH_LABELS[match] || '匹配';
}

/** 当前已选值（统一成数组，便于判断哪些未解析） */
const selectedValues = computed(() => {
  if (props.multiple) return Array.isArray(props.modelValue) ? props.modelValue : [];
  return props.modelValue ? [props.modelValue] : [];
});

/** 已选值里不在插件列表中的项（多半是手打的、插件不存在） */
const unresolved = computed(() => {
  const known = new Set(allPlugins.value.map((item) => item.value));
  return selectedValues.value.filter((name) => name && !known.has(name));
});

/**
 * 下拉选项。
 *
 * 未输入时给全部插件（便于直接浏览），输入后交给后端做模糊排序。
 * 已选项一并保留，这样 chips 能显示本地化展示名而不是裸注册名。
 */
const options = computed(() => allPlugins.value.slice(0, 200));

function toOption(plugin) {
  const descParts = [plugin.desc];
  if (!plugin.activated) descParts.push('（已停用）');
  if (plugin.reserved) descParts.push('（内置）');
  return {
    title: plugin.display_name
      ? `${plugin.display_name}（${plugin.name}）`
      : plugin.name,
    value: plugin.name,
    desc: descParts.filter(Boolean).join(' '),
    match: plugin.match || '',
  };
}

let searchTimer = null;

function onSearch(text) {
  searchText.value = text || '';
  if (searchTimer) clearTimeout(searchTimer);
  // 轻微防抖，避免每敲一个字母都请求一次
  searchTimer = setTimeout(() => {
    loadPlugins(searchText.value);
  }, 180);
}

async function loadPlugins(query = '') {
  loading.value = true;
  try {
    const res = await apiV1Client.get('/plugin-beta/plugins', {
      params: { q: query || '', limit: 200 },
    });
    const items = res?.data?.data?.items || [];
    allPlugins.value = items.map(toOption);
  } catch (error) {
    // 接口不可用时退化为纯手填，不阻塞页面
    allPlugins.value = [];
  } finally {
    loading.value = false;
  }
}

function onChange(value) {
  if (props.multiple) {
    const list = Array.isArray(value) ? value : [];
    emit('update:modelValue', list.map((item) => (typeof item === 'string' ? item : item?.value)).filter(Boolean));
    return;
  }
  if (value && typeof value === 'object') {
    emit('update:modelValue', value.value || '');
    return;
  }
  emit('update:modelValue', value || '');
}

onMounted(() => {
  loadPlugins('');
});
</script>

<style scoped>
.plugin-name-input {
  width: 100%;
}
</style>
