<script setup lang="ts">
import { onMounted, ref } from 'vue'
import MarketIcon from './MarketIcon.vue'
import { typeColor, typeLetter, typeName } from '../utils/market'

interface Cap {
  id: string
  name: string
  display_name?: string
  type: string
  version?: string
  description?: string
  category?: string
  tags?: string[]
  distribution?: string
  icon_url?: string
  joined?: boolean
  installed?: boolean
  verified?: boolean
  install_policy?: string
  rating_count?: number
  avg_rating?: number
  usage_count?: number
}

const props = defineProps<{ cap: Cap; canInstall?: boolean; enabled?: boolean }>()
const emit = defineEmits<{
  (e: 'open'): void
  (e: 'join'): void
  (e: 'leave'): void
  (e: 'install'): void
  (e: 'uninstall'): void
  (e: 'toggle', v: boolean): void
  (e: 'mounted', id: string): void
}>()

const c = props.cap
const title = (c.display_name || '').trim() || c.name
const isNew = !c.rating_count
// 属性标签：分类 + 能力自带 tags（不含功能性标记）
const attrTags = [
  ...(c.category ? [c.category] : []),
  ...(c.tags || []).filter((t) => t && t !== 'plugin-component')
]
const subline = c.version ? `v${c.version}` : ''

// 让父级知道卡片已挂载(用于按需加载图标)
onMounted(() => emit('mounted', c.id))
</script>

<template>
  <article class="cap-card">
    <span v-if="isNew" class="cap-ribbon">新品</span>
    <div class="card-head">
      <MarketIcon
        :id="c.id"
        :name="title"
        :size="44"
        :has-icon="!!c.icon_url"
        :letter="typeLetter(c.type)"
        :color="typeColor(c.type)"
      />
      <div class="head-text">
        <h3 class="cap-name" :title="title" @click="emit('open')">{{ title }}</h3>
        <div v-if="subline" class="head-sub">{{ subline }}</div>
      </div>
      <span class="cap-type" :class="'t-' + c.type">{{ typeName(c.type) }}</span>
    </div>

    <p class="cap-desc" @click="emit('open')">{{ c.description || '暂无描述' }}</p>

    <div v-if="attrTags.length" class="cap-tags">
      <span v-for="t in attrTags" :key="t" class="badge">{{ t }}</span>
    </div>

    <div class="cap-foot">
      <span class="foot-meta">
        <span v-if="c.rating_count" class="rating">★ {{ c.avg_rating || '暂无' }}<em>（{{ c.rating_count }}）</em></span>
        <span v-else class="muted">暂无评分</span>
        <span v-if="c.usage_count"> · {{ c.usage_count }} 次</span>
      </span>
      <div class="foot-actions">
        <template v-if="c.installed">
          <el-switch
            :model-value="enabled"
            inline-prompt
            active-text="启用"
            inactive-text="停用"
            size="small"
            @change="(v: string | number | boolean) => emit('toggle', !!v)"
          />
          <el-button size="small" type="danger" plain @click="emit('uninstall')">卸载</el-button>
        </template>
        <template v-else>
          <el-button v-if="!c.joined" size="small" type="primary" @click="emit('join')">加入</el-button>
          <el-button v-else size="small" plain @click="emit('leave')">移出</el-button>
          <el-button v-if="canInstall && c.joined" size="small" type="primary" @click="emit('install')">
            安装（云端托管）
          </el-button>
        </template>
      </div>
    </div>
  </article>
</template>

<style scoped>
.cap-card {
  position: relative;
  display: flex;
  flex-direction: column;
  background: var(--bg-card, #fff);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 14px 16px 0;
  transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
  overflow: hidden;
}
.cap-card:hover {
  transform: translateY(-2px);
  border-color: var(--el-color-primary-light-5);
  box-shadow: var(--el-box-shadow-light);
}
.card-head { display: flex; align-items: center; gap: 12px; }
.head-text { min-width: 0; flex: 1; }
.cap-name {
  margin: 0; font-size: 16px; font-weight: 650; letter-spacing: -0.01em;
  color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer;
}
.cap-name:hover { color: var(--el-color-primary); }
.head-sub { margin-top: 3px; font-size: 12px; color: var(--text-3); }
.cap-desc {
  color: var(--text-2); font-size: 13px; margin: 12px 0; cursor: pointer;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  min-height: 38px;
}
.cap-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; min-height: 24px; }
.cap-ribbon {
  position: absolute; top: 0; right: 0; z-index: 2;
  padding: 3px 10px; border-bottom-left-radius: 10px;
  font-size: 11px; font-weight: 650; color: #fff;
  background: linear-gradient(135deg, var(--el-color-primary), var(--el-color-success));
}
.cap-type {
  flex: none; align-self: flex-start;
  font-size: 11px; line-height: 18px; padding: 0 8px; border-radius: 999px;
  border: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-regular); background: var(--el-fill-color-light);
}
.t-agent { color: #2f6bff; background: #2f6bff14; border-color: #2f6bff55; }
.t-tool { color: #12b76a; background: #12b76a14; border-color: #12b76a55; }
.t-skill { color: #f5a524; background: #f5a52414; border-color: #f5a52455; }
.t-mcp { color: #7c3aed; background: #7c3aed14; border-color: #7c3aed55; }
.t-plugin { color: #e5484d; background: #e5484d14; border-color: #e5484d55; }
.t-workflow { color: #0ea5e9; background: #0ea5e914; border-color: #0ea5e955; }
.cap-foot {
  display: flex; justify-content: space-between; align-items: center; gap: 8px;
  margin-top: auto; font-size: 12px; padding: 10px 0;
  border-top: 1px solid var(--border);
}
.foot-meta { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--text-3); }
.rating { color: var(--el-color-warning); }
.rating em { font-style: normal; color: var(--text-3); margin-left: 2px; }
.rating.muted { color: var(--text-3); }
.foot-actions { display: flex; align-items: center; gap: 6px; flex: none; }
.badge {
  display: inline-flex; align-items: center; height: 22px; padding: 0 9px;
  border-radius: 999px; font-size: 12px; line-height: 1; white-space: nowrap;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
}
.badge-success {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
  border-color: var(--el-color-success-light-7);
}
.badge-primary {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-7);
}
.badge-warning {
  color: var(--el-color-warning);
  background: var(--el-color-warning-light-9);
  border-color: var(--el-color-warning-light-7);
}
</style>
