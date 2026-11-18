<template>
  <section class="page" data-module="quality">
    <header class="page-head">
      <div>
        <h2>质量报告</h2>
        <p class="page-desc">从已落库采集样本按指标口径聚合质量指标数据集，读端只消费已封存统计快照，刷新与重新进入保持一致。</p>
      </div>
      <button class="btn ghost" type="button" data-testid="refresh-all" @click="refreshAll">刷新</button>
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

    <!-- 统计周期切换 + 当前快照信息 -->
    <div class="panel">
      <div class="panel-head">
        <h3>统计窗口与当前快照</h3>
        <label class="period-switch">统计周期
          <select v-model="period" data-testid="period-select" @change="onPeriodChange">
            <option value="daily">按日</option>
            <option value="weekly">按周</option>
            <option value="monthly">按月</option>
          </select>
        </label>
      </div>
      <div v-if="currentWindow" class="snapshot-grid" data-testid="snapshot-panel">
        <div><span class="meta-label">窗口编号</span><strong data-testid="current-window-id">{{ currentWindow.window_id }}</strong></div>
        <div><span class="meta-label">当前快照编号</span><strong data-testid="current-snapshot-no">{{ currentSnapshot?.snapshot_no ?? '—' }}</strong></div>
        <div><span class="meta-label">样本数量</span><strong data-testid="current-sample-count">{{ currentWindow.sample_count }}</strong></div>
        <div><span class="meta-label">平均达标率</span><strong data-testid="current-pass-rate">{{ averagePassRate }}</strong></div>
        <div><span class="meta-label">生成时间</span><strong>{{ currentSnapshot?.generated_at ?? '—' }}</strong></div>
        <div>
          <span class="meta-label">快照状态</span>
          <span class="tag" :class="currentWindow.status === 'SEALED' ? 'tag-ok' : 'tag-busy'" data-testid="current-snapshot-status">{{ statusText(currentWindow.status) }}</span>
        </div>
        <div class="span-2">
          <span class="meta-label">重复打开快照比对</span>
          <strong data-testid="snapshot-compare">
            上次打开 {{ lastSeenSnapshotNo || '无记录' }} ／ 当前 {{ currentSnapshot?.snapshot_no ?? '—' }}
            <span :class="snapshotCompareTag.kind" class="tag">{{ snapshotCompareTag.text }}</span>
          </strong>
        </div>
      </div>
      <p v-else class="empty-hint">该周期暂无窗口</p>

      <table class="data-table" data-testid="windows-table">
        <thead>
          <tr><th>窗口编号</th><th>周期</th><th>样本数量</th><th>当前快照编号</th><th>生成时间</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="w in windows" :key="w.window_id" data-testid="window-row" :class="{ 'row-active': w.window_id === currentWindow?.window_id }" @click="selectWindow(w)">
            <td>{{ w.window_id }}</td>
            <td>{{ periodLabel(w.period) }}</td>
            <td>{{ w.sample_count }}</td>
            <td>{{ w.current_snapshot?.snapshot_no ?? '—' }}</td>
            <td>{{ w.current_snapshot?.generated_at ?? '—' }}</td>
            <td><span class="tag" :class="w.status === 'SEALED' ? 'tag-ok' : 'tag-busy'">{{ statusText(w.status) }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 样本登记与快照生成 -->
    <div class="panel">
      <div class="panel-head"><h3>样本登记与快照封存</h3></div>
      <form class="analysis-form" @submit.prevent="submitSample">
        <label><span>样本来源</span>
          <select v-model="sampleForm.source" data-testid="sample-source">
            <option value="raw">原始样本</option>
            <option value="supplement">补录样本（含迟到记录）</option>
          </select>
        </label>
        <label><span>指标值</span><input v-model="sampleForm.value" type="number" step="0.01" min="0" max="1" data-testid="sample-value" placeholder="0.00 ~ 1.00" /></label>
        <label><span>期望窗口版本（乐观锁，可空）</span><input v-model="sampleForm.expected_version" data-testid="sample-expected-version" :placeholder="`当前版本 ${currentWindow?.sample_version ?? ''}`" /></label>
        <button class="btn" type="submit" data-testid="submit-sample">登记样本</button>
      </form>
      <p class="form-hint">迟到采集记录进入所属统计窗口后，重新封存只生成该窗口的新快照；已封存快照不可原地重写。原始/补录样本同版本并发更新时返回 QUALITY_SAMPLE_VERSION_CONFLICT。</p>
      <div class="action-row">
        <button class="btn" type="button" data-testid="start-generation" :disabled="!!currentGeneratingJobId" @click="startGeneration">开始生成（GENERATING）</button>
        <button class="btn primary" type="button" data-testid="seal-snapshot" :disabled="!currentGeneratingJobId" @click="sealSnapshot">封存新快照</button>
        <span v-if="currentGeneratingJobId" class="tag tag-busy" data-testid="generating-job">生成中：{{ currentGeneratingJobId }}（读端不可用）</span>
      </div>
    </div>

    <!-- 质量指标数据集 -->
    <div class="panel">
      <div class="panel-head">
        <h3>质量指标数据集（持久化，刷新后保留原记录）</h3>
        <button class="btn primary" type="button" data-testid="submit-dataset" @click="submitDataset">提交当前快照为数据集</button>
      </div>
      <table class="data-table" data-testid="datasets-table">
        <thead><tr><th>数据集版本</th><th>内容指纹</th><th>窗口条目</th><th>生成时间</th><th>备注</th><th>当前版本</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="d in datasets" :key="d.dataset_id" data-testid="dataset-row">
            <td>{{ d.version }}</td>
            <td>{{ d.fingerprint }}</td>
            <td>{{ d.entry_count }}</td>
            <td>{{ d.created_at }}</td>
            <td>{{ d.remark || '—' }}</td>
            <td><span v-if="d.is_current" class="tag tag-ok">当前</span><span v-else>—</span></td>
            <td><button class="btn ghost" type="button" data-testid="use-dataset" :disabled="d.is_current" @click="useDataset(d.dataset_id)">切换到此版本</button></td>
          </tr>
        </tbody>
      </table>
      <p class="form-hint">并发或连续提交相同内容只保留一个成功版本：相同幂等键复用原版本，不同键返回 QUALITY_DATASET_CONFLICT，采集校验固定读取当前版本。</p>

      <div v-if="datasets.length >= 2" class="diff-box">
        <div class="panel-head"><h4>版本差异（沿采集校验数据流）</h4></div>
        <div class="action-row">
          <label>旧版
            <select v-model.number="diffForm.base" data-testid="diff-base"><option v-for="d in datasets" :key="d.dataset_id" :value="d.dataset_id">{{ d.version }}</option></select>
          </label>
          <label>新版
            <select v-model.number="diffForm.target" data-testid="diff-target"><option v-for="d in datasets" :key="d.dataset_id" :value="d.dataset_id">{{ d.version }}</option></select>
          </label>
          <button class="btn" type="button" data-testid="run-diff" @click="runDiff">定位差异</button>
        </div>
        <div v-if="diffResult" data-testid="diff-result">
          <p>变化窗口：<strong>{{ diffResult.window_diffs.map(w => w.window_id).join('、') || '无' }}</strong></p>
          <p>新增阻断窗口：<strong class="trend-down">{{ diffResult.validation_downstream.newly_blocked_windows.join('、') || '无' }}</strong></p>
          <p>解除阻断窗口：<strong class="trend-up">{{ diffResult.validation_downstream.released_windows.join('、') || '无' }}</strong></p>
        </div>
      </div>
    </div>

    <!-- 运行分析（沿用既有调用契约） -->
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
    <p v-if="!results.length" class="empty-hint">运行分析只读取已封存快照，页面不自行补算缺失样本</p>

    <!-- 校验趋势记录台账 -->
    <div class="panel">
      <div class="panel-head"><h3>同步到采集校验的校验趋势记录台账</h3></div>
      <table class="data-table" data-testid="ledger-table">
        <thead><tr><th>台账编号</th><th>数据集版本</th><th>触发原因</th><th>窗口数</th><th>异常窗口数</th><th>平均达标率</th><th>记录时间</th></tr></thead>
        <tbody><tr v-for="t in trendLedger" :key="t.ledger_id"><td>{{ t.ledger_id }}</td><td>{{ t.dataset_version }}</td><td>{{ t.reason }}</td><td>{{ t.window_count }}</td><td>{{ t.abnormal_window_count }}</td><td>{{ t.average_pass_rate }}</td><td>{{ t.recorded_at }}</td></tr></tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/quality'
const PERIOD_LABELS: Record<string, string> = { daily: '按日', weekly: '按周', monthly: '按月' }

interface SnapshotMeta { snapshot_id: number; snapshot_no: string; generated_at: string; sample_count: number }
interface WindowRow { window_id: string; period: string; sample_count: number; sample_version: number; status: string; generating_job_id: string | null; current_snapshot: SnapshotMeta | null }
interface DatasetRow { dataset_id: number; version: string; fingerprint: string; status: string; remark: string; created_at: string; entry_count: number; is_current: boolean }
interface LedgerRow { ledger_id: number; dataset_version: string; reason: string; window_count: number; abnormal_window_count: number; average_pass_rate: number; recorded_at: string }
interface RunRow { id: number; 分组: string; 指标值: number; 对比基准: number; 差异: number; 窗口编号?: string; 快照编号?: string; 样本数量?: number; 达标率?: number; [key: string]: unknown }
interface DiffWindow { window_id: string; change_type: string }
interface DiffResult { window_diffs: DiffWindow[]; validation_downstream: { newly_blocked_windows: string[]; released_windows: string[] } }
interface Notice { kind: 'ok' | 'error'; message: string }

const summaryMetrics = ref<{ label: string; value: number; trend: number }[]>([])
const windows = ref<WindowRow[]>([])
const datasets = ref<DatasetRow[]>([])
const trendLedger = ref<LedgerRow[]>([])
const results = ref<RunRow[]>([])
const chartData = ref<{ label: string; value: number }[]>([])
const period = ref('daily')
const selectedWindowId = ref<string | null>(null)
const currentGeneratingJobId = ref<string | null>(null)
const snapshotDetail = ref<any>(null)
const notice = reactive<Notice>({ kind: 'ok', message: '' })

const sampleForm = reactive({ source: 'raw', value: '', expected_version: '' })
const diffForm = reactive<{ base: number | null; target: number | null }>({ base: null, target: null })
const diffResult = ref<DiffResult | null>(null)

const params = ref<Record<string, string>>({ 统计周期: '按日' })
const paramFields = ['统计周期', '统计口径', '阈值下界', '阈值上界']
const options: Record<string, string[]> = { 统计周期: ['按日', '按周', '按月'], 统计粒度: ['按日', '按周', '按月'] }
const resultColumns = ['分组', '快照编号', '窗口编号', '样本数量', '指标值', '对比基准', '差异', '达标率']

const currentWindow = computed<WindowRow | null>(() => windows.value.find(w => w.window_id === selectedWindowId.value) ?? windows.value[0] ?? null)
const currentSnapshot = computed<SnapshotMeta | null>(() => currentWindow.value?.current_snapshot ?? null)
const averagePassRate = computed(() => {
  const records: any[] = snapshotDetail.value?.metric_records ?? []
  if (!records.length) return '—'
  const avg = records.reduce((sum, r) => sum + Number(r.达标率 || 0), 0) / records.length
  return `${(avg * 100).toFixed(1)}%`
})
const lastSeenSnapshotNo = computed(() => {
  if (!currentWindow.value) return ''
  return localStorage.getItem(`quality:last-snapshot:${currentWindow.value.window_id}`) ?? ''
})
const snapshotCompareTag = computed(() => {
  const now = currentSnapshot.value?.snapshot_no
  if (!now) return { text: '无快照', kind: 'tag-busy' }
  if (!lastSeenSnapshotNo.value) return { text: '首次打开', kind: 'tag-ok' }
  return lastSeenSnapshotNo.value === now ? { text: '快照一致', kind: 'tag-ok' } : { text: '已生成新快照', kind: 'tag-busy' }
})

function periodLabel(p: string) { return PERIOD_LABELS[p] ?? p }
function statusText(s: string) { return s === 'SEALED' ? '已封存' : s === 'GENERATING' ? '生成中' : '无快照' }
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

async function fetchSnapshotDetail() {
  const cur = currentWindow.value
  if (!cur || cur.status !== 'SEALED' || !cur.current_snapshot) { snapshotDetail.value = null; return }
  const res = await request(`${ENDPOINT}/snapshots/${cur.current_snapshot.snapshot_id}`)
  if (res.ok) snapshotDetail.value = await res.json()
}

async function fetchWindows() {
  const res = await request(`${ENDPOINT}/windows?period=${period.value}`)
  if (!res.ok) { flash('error', await readError(res)); return }
  windows.value = (await res.json()).items || []
  if (!selectedWindowId.value || !windows.value.some(w => w.window_id === selectedWindowId.value)) {
    selectedWindowId.value = windows.value[0]?.window_id ?? null
  }
  const cur = currentWindow.value
  currentGeneratingJobId.value = cur?.generating_job_id ?? null
  // 记录本次打开看到的快照编号，供下次重复打开时比较。
  if (cur?.current_snapshot) localStorage.setItem(`quality:last-snapshot:${cur.window_id}`, cur.current_snapshot.snapshot_no)
  await fetchSnapshotDetail()
}

async function fetchDatasets() {
  const res = await request(`${ENDPOINT}/datasets`)
  if (res.ok) datasets.value = (await res.json()).items || []
}

async function fetchLedger() {
  const res = await request(`${ENDPOINT}/trend-ledger`)
  if (res.ok) trendLedger.value = (await res.json()).items || []
}

async function refreshAll() {
  await Promise.all([fetchSummary(), fetchWindows(), fetchDatasets(), fetchLedger()])
  flash('ok', '已按已封存快照重新读取，结果与上次一致')
}

function onPeriodChange() {
  selectedWindowId.value = null
  params.value.统计周期 = PERIOD_LABELS[period.value]
  fetchWindows()
}

function selectWindow(w: WindowRow) { selectedWindowId.value = w.window_id }

async function submitSample() {
  const payload: Record<string, unknown> = { period: period.value, source: sampleForm.source, value: Number(sampleForm.value) }
  if (currentWindow.value) payload.window_id = currentWindow.value.window_id
  if (sampleForm.expected_version !== '') payload.expected_version = Number(sampleForm.expected_version)
  const res = await request(`${ENDPOINT}/samples`, { method: 'POST', body: JSON.stringify({ values: payload }) })
  if (!res.ok) { flash('error', await readError(res)); return }
  flash('ok', '采集样本已落库，封存后将进入新快照')
  sampleForm.value = ''
  sampleForm.expected_version = ''
  await fetchWindows()
}

async function startGeneration() {
  const payload: Record<string, unknown> = { period: period.value }
  if (currentWindow.value) payload.window_id = currentWindow.value.window_id
  const res = await request(`${ENDPOINT}/snapshots/generate`, { method: 'POST', body: JSON.stringify({ values: payload }) })
  if (!res.ok) { flash('error', await readError(res)); return }
  const job = await res.json()
  currentGeneratingJobId.value = job.job_id
  flash('ok', `快照生成任务已创建：${job.job_id}，GENERATING 期间读端不可用`)
  await fetchWindows()
}

async function sealSnapshot() {
  if (!currentGeneratingJobId.value) {
    flash('error', '请先开始生成任务')
    return
  }
  const res = await request(`${ENDPOINT}/snapshots/seal`, { method: 'POST', body: JSON.stringify({ values: { job_id: currentGeneratingJobId.value } }) })
  if (!res.ok) { flash('error', await readError(res)); return }
  const snap = await res.json()
  currentGeneratingJobId.value = null
  flash('ok', `已封存新快照 ${snap.snapshot_no}（样本 ${snap.sample_count}），旧快照保持不变`)
  await Promise.all([fetchWindows(), fetchLedger()])
}

async function submitDataset() {
  const sids = windows.value.filter(w => w.current_snapshot).map(w => w.current_snapshot!.snapshot_id)
  if (!sids.length) { flash('error', '没有可提交的已封存快照'); return }
  // 幂等键在前端会话内固定，连续点两次提交的是同一份提交物，只保留一个成功版本。
  const idempotencyKey = `ui-${period.value}-${sids.join('-')}`
  const res = await request(`${ENDPOINT}/datasets`, { method: 'POST', body: JSON.stringify({ values: { snapshot_ids: sids, idempotency_key: idempotencyKey } }) })
  if (!res.ok) { flash('error', await readError(res)); await fetchDatasets(); return }
  const body = await res.json()
  flash('ok', body.item?.deduped ? `复用已存在的成功版本 ${body.item.version}` : `数据集已封存：${body.item.version}`)
  await Promise.all([fetchDatasets(), fetchLedger()])
}

async function useDataset(id: number) {
  const res = await request(`${ENDPOINT}/datasets/use`, { method: 'POST', body: JSON.stringify({ values: { dataset_id: id } }) })
  if (!res.ok) { flash('error', await readError(res)); return }
  const body = await res.json()
  flash('ok', `质量报告已切换到 ${body.version}，采集校验下游结果随之变化`)
  await Promise.all([fetchDatasets(), fetchLedger()])
}

async function runDiff() {
  if (diffForm.base == null || diffForm.target == null) { flash('error', '请选择旧版与新版数据集'); return }
  const res = await request(`${ENDPOINT}/datasets/diff?base=${diffForm.base}&target=${diffForm.target}`)
  if (!res.ok) { flash('error', await readError(res)); return }
  diffResult.value = await res.json()
}

async function runAnalysis() {
  const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) })
  if (!res.ok) { flash('error', await readError(res)); results.value = []; return }
  const items: RunRow[] = (await res.json()).items || []
  results.value = items
  chartData.value = items.slice(0, 6).map(r => ({ label: r.快照编号 || r.分组, value: Number(r.指标值) || 0 }))
}

onMounted(async () => {
  await Promise.all([fetchSummary(), fetchWindows(), fetchDatasets(), fetchLedger()])
})
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 14px 0; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.panel-head h3, .panel-head h4 { margin: 0; font-size: 15px; }
.period-switch { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.snapshot-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px 16px; padding: 10px 0 14px; }
.snapshot-grid .span-2 { grid-column: span 2; }
.meta-label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 2px; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; border: 1px solid var(--border); }
.tag-ok { color: #15803d; background: #f0fdf4; border-color: #bbf7d0; }
.tag-busy { color: #b45309; background: #fffbeb; border-color: #fde68a; }
.notice { padding: 10px 14px; border-radius: 8px; margin: 10px 0; font-size: 13px; }
.notice.ok { background: #f0fdf4; border: 1px solid #bbf7d0; color: #15803d; }
.notice.error { background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; }
.action-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
.form-hint, .empty-hint { font-size: 12px; color: var(--muted); margin: 8px 0 0; }
.row-active { background: #f8fafc; }
[data-testid='window-row'] { cursor: pointer; }
.diff-box { margin-top: 14px; border-top: 1px dashed var(--border); padding-top: 12px; }
</style>
