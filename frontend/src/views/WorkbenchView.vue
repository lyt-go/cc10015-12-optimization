<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <!-- 补传候选队列：只在数据传输模块出现，按目标系统分组给岸端值班员定位 -->
    <section v-if="meta.key === 'data_transfer'" class="supplement-panel" data-testid="supplement-candidates">
      <header class="supplement-head">
        <h3>补传候选队列（{{ candidateSummary.total }} 条）</h3>
        <span class="hint">只列已失败且仍有重试资格的传输；科研归档端与岸端分别核对</span>
      </header>
      <div class="supplement-groups">
        <article v-for="(count, target) in candidateSummary.by_target" :key="target" class="supplement-group">
          <h4>{{ target }}：{{ count }} 条</h4>
          <ul>
            <li v-for="candidate in candidatesFor(target)" :key="candidate.id" :data-testid="`candidate-${candidate.id}`">
              <span class="candidate-main">{{ candidate['数据内容'] }} · 第{{ candidate['轮次'] }}轮 · {{ candidate.attempt_id }}</span>
              <span class="candidate-meta">
                航次 {{ candidate['航次编号'] || '—' }} · 回执 {{ candidate['领取回执号'] || '未领取' }}
              </span>
              <button class="link" type="button" :data-testid="`claim-${candidate.id}`" @click="claimCandidate(candidate)">
                领取补传
              </button>
            </li>
          </ul>
        </article>
        <p v-if="!candidateSummary.total" class="empty-state">当前没有可办理的补传候选</p>
      </div>
    </section>

    <!-- 汇编报告补件凭据：只在成果汇编模块出现 -->
    <section v-if="meta.key === 'results_compile' && supplementInfo" class="supplement-panel" data-testid="supplement-credential">
      <header class="supplement-head">
        <h3>报告 #{{ supplementInfo.report_id }} 补件凭据</h3>
        <span class="hint">{{ supplementInfo['补件状态'] || '尚未办理补传' }}</span>
      </header>
      <p v-if="supplementInfo.credential" class="credential-line">
        当前唯一有效凭据：{{ supplementInfo.credential['来源'] }} · {{ supplementInfo.credential['attempt_id'] }}
        · 第{{ supplementInfo.credential['轮次'] }}轮 · {{ supplementInfo.credential['目标系统'] }}
        <span v-if="supplementInfo.credential['重复来件']">（重复来件，未重复核收）</span>
      </p>
      <p v-else class="hint">尚无岸端接收凭据</p>
      <p v-if="supplementInfo['科研归档接收']" class="hint">
        科研归档端已收到（{{ supplementInfo['科研归档接收']['attempt_id'] }}），不能替代岸端接收
      </p>
      <p v-if="supplementInfo['最近失败传输']" class="hint">
        实际失败传输：{{ supplementInfo['最近失败传输']['传输记录id'] }}（{{ supplementInfo['最近失败传输']['目标系统'] }}，
        {{ supplementInfo['最近失败传输']['attempt_id'] }}）
      </p>
      <ul class="receipt-list">
        <li v-for="receipt in supplementInfo.receipts" :key="String(receipt['回执号'])">
          回执 {{ receipt['回执号'] }} · {{ receipt['办理状态'] }}
          <span v-if="receipt['冲突说明']">· {{ receipt['冲突说明'] }}</span>
        </li>
      </ul>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button v-for="action in meta.actions" :key="action" class="link" type="button"
              :data-testid="`action-${row.id}-${action}`" @click="runAction(action, row)">{{ action }}</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">暂无{{ meta.name }}数据，可先登记{{ meta.entity }}</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  claimSupplement,
  createEntry,
  downloadEntries,
  fetchSupplementInfo,
  listEntries,
  listSupplementCandidates,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { SupplementInfo, SupplementSummary } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const candidateSummary = ref<SupplementSummary>({ items: [], total: 0, by_target: {} })
const supplementInfo = ref<SupplementInfo | null>(null)

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((r) => r.status === meta.statuses[1]).length },
  { label: meta.metrics[2], value: rows.value.filter((r) => r.status === meta.statuses[meta.statuses.length - 1]).length },
])

const statusSummary = computed(() =>
  meta.statuses.map((s) => ({ status: s, count: rows.value.filter((r) => String(r.status) === s).length })),
)

function candidatesFor(target: string) {
  return candidateSummary.value.items.filter((item) => String(item['目标系统'] || '未指定') === target)
}

async function loadSupplement() {
  if (meta.key === 'data_transfer') {
    candidateSummary.value = await listSupplementCandidates()
  } else if (meta.key === 'results_compile') {
    // 优先展示需补充 / 有补件记录的报告，没有就退回第一条
    const target = rows.value.find((r) => r.status === '需补充') ?? rows.value.find((r) => Number(r.id) > 0)
    supplementInfo.value = target ? await fetchSupplementInfo(Number(target.id)) : null
  }
}

async function claimCandidate(candidate: { id: number }) {
  errorMessage.value = ''
  try {
    const r = await claimSupplement(candidate.id, { 值班员: '岸端值班员' })
    if (!r.ok) throw new Error(r.message)
    await reload()
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '领取补传失败' }
}

function resetFilters() { filters.value = {}; void reload() }

async function exportRows() {
  errorMessage.value = ''
  try { await downloadEntries(meta.key, filters.value) }
  catch (e) { errorMessage.value = e instanceof Error ? e.message : '导出失败' }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((f) => [f, '']))
  errorMessage.value = ''; showCreate.value = true
}

async function submitCreate() {
  creating.value = true; errorMessage.value = ''
  try {
    const r = await createEntry(meta.key, createValues.value)
    if (!r.ok) throw new Error(r.message)
    showCreate.value = false; await reload()
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '登记失败' }
  finally { creating.value = false }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const r = await applyAction(meta.key, Number(row.id), action)
    if (!r.ok) throw new Error(r.message)
    await reload()
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '操作失败' }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items; total.value = payload.total
    await loadSupplement()
  } catch (e) { errorMessage.value = e instanceof Error ? e.message : '业务列表读取失败' }
}

onMounted(reload)
</script>

<style scoped>
.supplement-panel { border:1px solid var(--border-color,#e2e8f0); border-radius:8px; padding:16px; margin-bottom:16px; background:#f8fafc; }
.supplement-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; }
.supplement-head h3 { margin:0 0 8px; font-size:15px; }
.supplement-groups { display:flex; flex-wrap:wrap; gap:16px; }
.supplement-group { min-width:280px; flex:1; }
.supplement-group h4 { margin:0 0 6px; font-size:13px; color:var(--text-secondary,#4a5568); }
.supplement-group ul { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:8px; }
.supplement-group li { display:flex; flex-direction:column; gap:2px; background:#fff; border:1px solid var(--border-color,#e2e8f0); border-radius:6px; padding:8px 10px; }
.candidate-main { font-size:13px; }
.candidate-meta { font-size:12px; color:var(--text-secondary,#4a5568); }
.hint { font-size:12px; color:var(--text-secondary,#4a5568); }
.credential-line { font-size:13px; margin:8px 0; }
.receipt-list { list-style:none; margin:8px 0 0; padding:0; font-size:12px; color:var(--text-secondary,#4a5568); }
.modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.35); display:flex; align-items:center; justify-content:center; z-index:100; }
.modal { background:#fff; border-radius:8px; padding:24px; width:480px; max-height:80vh; overflow-y:auto; }
.create-field { display:flex; flex-direction:column; gap:4px; margin-bottom:12px; }
.create-field span { font-size:13px; color:var(--text-secondary,#4a5568); }
.create-field input { padding:6px 8px; border:1px solid var(--border-color,#e2e8f0); border-radius:4px; font-size:13px; }
</style>