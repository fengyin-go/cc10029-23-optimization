<template>
  <section class="page" data-module="validate">
    <header class="page-head">
      <div>
        <h2>采集校验</h2>
        <p class="page-desc">校验采集结果的行数、格式、空值和重复；待办流只读取质量报告已封存的质量指标数据集，正在生成的指标不可读。</p>
      </div>
      <button class="btn ghost" type="button" data-testid="refresh-validate" @click="refreshAll">刷新</button>
    </header>

    <div v-if="notice.message" class="notice" :class="notice.kind" data-testid="notice-banner">
      <span>{{ notice.kind === 'error' ? '✕' : '✓' }} {{ notice.message }}</span>
    </div>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend > 0 ? '↑' : m.trend < 0 ? '↓' : '—' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>

    <!-- 质量指标待办流 -->
    <div class="panel">
      <div class="panel-head">
        <h3>质量指标待办流（来源：已封存数据集 {{ datasetVersion || '—' }}）</h3>
        <span class="tag" :class="todos.length ? 'tag-busy' : 'tag-ok'" data-testid="todo-count">待处理阻断项 {{ pendingCount }}</span>
      </div>
      <table class="data-table" data-testid="todo-table">
        <thead><tr><th>待办编号</th><th>异常窗口</th><th>快照编号</th><th>数据集版本</th><th>阻断级别</th><th>异常指标</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="t in todos" :key="t.todo_id" data-testid="todo-row" :class="{ 'row-done': t.status === 'DONE' }">
            <td>{{ t.todo_id }}</td>
            <td>{{ t.window_id }}</td>
            <td>{{ t.snapshot_no }}</td>
            <td>{{ t.dataset_version }}</td>
            <td><span class="tag tag-busy">{{ t.level }}</span></td>
            <td>
              <span v-for="m in t.detail.abnormal_metrics" :key="m.指标编号" class="metric-chip">
                {{ m.指标编号 }} 达标率 {{ m.达标率 }}
              </span>
            </td>
            <td>{{ t.status === 'DONE' ? '已写回' : '待处理' }}</td>
            <td>
              <button class="btn primary" type="button" data-testid="writeback-btn" :disabled="t.status === 'DONE'" @click="writeback(t.todo_id)">写回阻断项</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="!todos.length" class="empty-hint">当前数据集没有异常窗口；采集校验不会读取 GENERATING 中的指标。</p>
    </div>

    <!-- 运行分析（沿用既有调用契约，数据来自已封存快照） -->
    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')||field.includes('周期')" v-model="params[field]">
          <option v-for="o in options[field] || []" :key="o" :value="o">{{ o }}</option>
        </select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length" class="empty-hint">运行分析仅展示已封存质量快照，不自行补算缺失样本</p>

    <!-- 阻断项写回台账 -->
    <div class="panel">
      <div class="panel-head"><h3>阻断项写回校验记录台账</h3></div>
      <table class="data-table" data-testid="writeback-table">
        <thead><tr><th>写回编号</th><th>关联待办</th><th>异常窗口</th><th>数据集版本</th><th>校验规则</th><th>阻断项</th><th>写回时间</th></tr></thead>
        <tbody>
          <tr v-for="w in writebacks" :key="w.writeback_id">
            <td>{{ w.writeback_id }}</td><td>{{ w.todo_id }}</td><td>{{ w.window_id }}</td><td>{{ w.dataset_version }}</td>
            <td>{{ w.rule_code ?? '—' }}</td>
            <td><span v-for="(m, i) in w.blocking_items" :key="i" class="metric-chip">{{ m.指标编号 }}</span></td>
            <td>{{ w.written_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!writebacks.length" class="empty-hint">尚无写回记录</p>
    </div>

    <!-- 校验趋势记录台账 -->
    <div class="panel">
      <div class="panel-head"><h3>校验趋势记录台账（质量指标数据集结论同步）</h3></div>
      <table class="data-table" data-testid="validate-ledger-table">
        <thead><tr><th>台账编号</th><th>数据集版本</th><th>触发原因</th><th>窗口数</th><th>异常窗口数</th><th>平均达标率</th><th>记录时间</th></tr></thead>
        <tbody><tr v-for="t in trendLedger" :key="t.ledger_id"><td>{{ t.ledger_id }}</td><td>{{ t.dataset_version }}</td><td>{{ t.reason }}</td><td>{{ t.window_count }}</td><td>{{ t.abnormal_window_count }}</td><td>{{ t.average_pass_rate }}</td><td>{{ t.recorded_at }}</td></tr></tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/validate'

interface AbnormalMetric { 指标编号: string; 达标率: number; 阈值下界: number; 阈值上界: number }
interface Todo { todo_id: number; window_id: string; snapshot_no: string; dataset_version: string; level: string; status: string; detail: { abnormal_metrics: AbnormalMetric[] } }
interface Writeback { writeback_id: number; todo_id: number; window_id: string; dataset_version: string; rule_code: string | null; blocking_items: AbnormalMetric[]; written_at: string }
interface LedgerRow { ledger_id: number; dataset_version: string; reason: string; window_count: number; abnormal_window_count: number; average_pass_rate: number; recorded_at: string }
interface RunRow { id: number; 分组: string; 指标值: number; 对比基准: number; 差异: number; 快照编号?: string; 窗口编号?: string; [key: string]: unknown }
interface Notice { kind: 'ok' | 'error'; message: string }

const summaryMetrics = ref<{ label: string; value: number; trend: number }[]>([])
const todos = ref<Todo[]>([])
const writebacks = ref<Writeback[]>([])
const trendLedger = ref<LedgerRow[]>([])
const datasetVersion = ref<string | null>(null)
const results = ref<RunRow[]>([])
const chartData = ref<{ label: string; value: number }[]>([])
const notice = reactive<Notice>({ kind: 'ok', message: '' })

const params = ref<Record<string, string>>({})
const paramFields = ['统计周期', '校验字段', '异常策略', '阻断级别']
const options: Record<string, string[]> = { 统计周期: ['按日', '按周', '按月'], 异常策略: ['阻断', '告警'], 阻断级别: ['一级', '二级'] }
const resultColumns = ['分组', '快照编号', '窗口编号', '指标值', '对比基准', '差异']

const pendingCount = computed(() => todos.value.filter(t => t.status === 'PENDING').length)

function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
function flash(kind: 'ok' | 'error', message: string) { notice.kind = kind; notice.message = message; window.setTimeout(() => { notice.message = '' }, 6000) }

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json()
    const detail = body?.detail
    if (detail && typeof detail === 'object' && detail.code) return `${detail.code}：${detail.message}`
    return typeof detail === 'string' ? detail : `请求失败（${response.status}）`
  } catch {
    return `请求失败（${response.status}）`
  }
}

async function fetchSummary() {
  const res = await request(`${ENDPOINT}/summary`)
  if (res.ok) summaryMetrics.value = (await res.json()).metrics || []
}

async function fetchTodos() {
  const res = await request(`${ENDPOINT}/quality-todos`)
  if (!res.ok) { flash('error', await readError(res)); return }
  const body = await res.json()
  todos.value = body.todos || []
  datasetVersion.value = body.dataset_version
}

async function fetchWritebacks() {
  const res = await request(`${ENDPOINT}/writebacks`)
  if (res.ok) writebacks.value = (await res.json()).items || []
}

async function fetchLedger() {
  const res = await request(`${ENDPOINT}/trend-ledger`)
  if (res.ok) trendLedger.value = (await res.json()).items || []
}

async function refreshAll() {
  await Promise.all([fetchSummary(), fetchTodos(), fetchWritebacks(), fetchLedger()])
}

async function writeback(todoId: number) {
  const res = await request(`${ENDPOINT}/quality-todos/${todoId}/writeback`, { method: 'POST', body: JSON.stringify({ values: {} }) })
  if (!res.ok) { flash('error', await readError(res)); return }
  const body = await res.json()
  flash('ok', `阻断项已写回校验记录：${body.item?.rule_code ?? '默认规则'}`)
  await Promise.all([fetchTodos(), fetchWritebacks()])
}

async function runAnalysis() {
  const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) })
  if (!res.ok) { flash('error', await readError(res)); results.value = []; return }
  const items: RunRow[] = (await res.json()).items || []
  results.value = items
  chartData.value = items.slice(0, 6).map(r => ({ label: r.快照编号 || r.分组, value: Number(r.指标值) || 0 }))
}

onMounted(refreshAll)
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 14px 0; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.panel-head h3 { margin: 0; font-size: 15px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid var(--border); }
.tag-ok { color: #15803d; background: #f0fdf4; border-color: #bbf7d0; }
.tag-busy { color: #b45309; background: #fffbeb; border-color: #fde68a; }
.notice { padding: 10px 14px; border-radius: 8px; margin: 10px 0; font-size: 13px; }
.notice.ok { background: #f0fdf4; border: 1px solid #bbf7d0; color: #15803d; }
.notice.error { background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; }
.metric-chip { display: inline-block; background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 1px 6px; margin: 1px 4px 1px 0; font-size: 12px; }
.empty-hint { font-size: 12px; color: var(--muted); margin: 8px 0 0; }
.row-done { opacity: 0.6; }
</style>
