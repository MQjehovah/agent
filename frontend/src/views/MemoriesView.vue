<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import MemoriesPanel from './ops/MemoriesPanel.vue'

const route = useRoute()
const viewAll = computed(() => route.query.view === 'all')
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div><h2>记忆管理</h2>
        <div class="sub">{{ viewAll ? '全部用户私有 + 全局公共记忆（运维全量）' : '仅展示我的私有记忆（个人空间，不含全局公共记忆）' }}</div>
      </div>
    </div>
    <MemoriesPanel :view-all="viewAll" />
  </div>
</template>
