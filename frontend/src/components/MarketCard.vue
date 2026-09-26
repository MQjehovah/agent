<script setup lang="ts">
import { onMounted, ref } from 'vue'
import MarketIcon from './MarketIcon.vue'
import { distName, gradeOf, policyName, typeColor, typeLetter, typeName } from '../utils/market'

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
const grade = gradeOf(c)
const tags = (c.tags || []).filter((t) => t && t !== 'plugin-component').slice(0, 2)
const policy = c.install_policy || 'optional'

// 让父级知道卡片已挂载(用于按需加载图标)
onMounted(() => emit('mounted', c.id))
</script>

<template>
  <article class="cap-card">
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
        <div class="head-sub">
          {{ typeName(c.type) }}<span v-if="c.version"> · v{{ c.version }}</span>
        </div>
      </div>
    </div>

    <p class="cap-desc" @click="emit('open')">{{ c.description || '暂无描述' }}</p>

    <div class="cap-tags">
      <span class="badge" :class="grade.cls">{{ grade.label }}</span>
      <span class="badge" :class="c.distribution === 'remote' ? 'badge-primary' : c.distribution === 'local' ? 'badge' : 'badge-success'">
        {{ distName(c.distribution) }}
      </span>
      <span v-if="c.verified" class="badge badge-success">认证</span>
      <span v-if="c.category" class="badge">{{ c.category }}</span>
      <span v-if="policy !== 'optional'" class="badge badge-warning">{{ policyName(policy) }}</span>
      <span v-for="t in tags" :key="t" class="badge">{{ t }}</span>
    </div>

    <div class="cap-foot">
      <span class="foot-meta">
        <span v-if="c.rating_count" class="rating">★ {{ c.avg_rating || '暂无' }}<em>（{{ c.rating_count }}）</em></span>
        <span v-else class="rating muted">暂无评分</span>
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
