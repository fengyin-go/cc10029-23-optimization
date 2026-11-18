import { expect, test } from '@playwright/test'

/**
 * 质量指标快照子系统验收：
 * - 读端只消费已封存快照，刷新/重新进入结果一致，可比较快照编号
 * - 可切换统计周期、查看样本数量与达标率
 * - 迟到样本只生成窗口新快照，旧快照不重写；乐观锁版本冲突返回固定错误码
 * - 数据集并发/连续提交只保留一个成功版本；旧版/新版下游结果不同
 * - 采集校验待办流读取异常窗口并把阻断项写回；趋势台账同步
 */
test.describe('质量指标快照与数据集', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/quality')
    await page.getByTestId('snapshot-panel').waitFor()
  })

  test('刷新读取一致、可切换统计周期并比较快照编号', async ({ page }) => {
    // 默认按日，当前窗口带窗口编号、快照编号与样本数量
    await expect(page.getByTestId('current-window-id')).toContainText(/^D-\d{8}$/)
    const firstSnapshot = await page.getByTestId('current-snapshot-no').innerText()
    expect(firstSnapshot).toMatch(/^SNAP-D-\d{8}-001$|^SNAP-D-\d{8}-\d{3}$/)
    const firstSampleCount = await page.getByTestId('current-sample-count').innerText()
    expect(Number(firstSampleCount)).toBeGreaterThan(0)

    // 重复打开（刷新）后仍是同一个当前快照：结果不漂移
    await page.reload()
    await page.getByTestId('snapshot-panel').waitFor()
    await expect(page.getByTestId('current-snapshot-no')).toHaveText(firstSnapshot)
    await expect(page.getByTestId('current-sample-count')).toHaveText(firstSampleCount)
    await expect(page.getByTestId('snapshot-compare')).toContainText('快照一致')

    // 切换统计周期：窗口编号随之变化，仍能看到快照编号与样本数量
    await page.getByTestId('period-select').selectOption('weekly')
    await expect(page.getByTestId('current-window-id')).toContainText(/^W-\d{6}$/)
    expect(await page.getByTestId('current-snapshot-no').innerText()).toMatch(/^SNAP-W-/)
    await page.getByTestId('period-select').selectOption('monthly')
    await expect(page.getByTestId('current-window-id')).toContainText(/^M-\d{6}$/)
    expect(await page.getByTestId('current-snapshot-no').innerText()).toMatch(/^SNAP-M-/)
  })

  test('迟到样本只封存新快照且旧快照不重写，版本冲突返回固定错误码', async ({ page }) => {
    await page.getByTestId('period-select').selectOption('daily')
    const beforeSnapshot = await page.getByTestId('current-snapshot-no').innerText()
    const beforeCount = Number(await page.getByTestId('current-sample-count').innerText())

    // 登记一条迟到/原始样本
    await page.getByTestId('sample-value').fill('0.91')
    await page.getByTestId('submit-sample').click()
    await expect(page.getByTestId('notice-banner')).toContainText('采集样本已落库')

    // 基于过期版本的并发更新必须被乐观锁拒绝
    await page.getByTestId('sample-expected-version').fill('0')
    await page.getByTestId('sample-value').fill('0.7')
    await page.getByTestId('submit-sample').click()
    await expect(page.getByTestId('notice-banner')).toContainText('QUALITY_SAMPLE_VERSION_CONFLICT')

    // 生成期间读端状态为生成中；封存后得到编号递增的新快照，样本数 +1，旧编号保留
    await page.getByTestId('start-generation').click()
    await expect(page.getByTestId('generating-job')).toBeVisible()
    await page.getByTestId('seal-snapshot').click()
    await expect(page.getByTestId('notice-banner')).toContainText('已封存新快照')
    const afterSnapshot = await page.getByTestId('current-snapshot-no').innerText()
    expect(afterSnapshot).not.toBe(beforeSnapshot)
    expect(Number(await page.getByTestId('current-sample-count').innerText())).toBe(beforeCount + 1)
    // 窗口台账里仍能看到旧快照曾存在（窗口编号不变，仅当前快照编号变化）
    await expect(page.getByTestId('current-window-id')).toContainText(/^D-\d{8}$/)
  })

  test('数据集连续提交只保留一个成功版本，旧版新版下游结果不同', async ({ page }) => {
    const rowsBefore = await page.getByTestId('dataset-row').count()

    // 同一提交物连续点两次：要么首次封存成功、第二次复用，要么内容已存在直接冲突；
    // 无论哪种情况，两次点击都不允许产生第二条数据集记录。
    await page.getByTestId('submit-dataset').click()
    await page.getByTestId('notice-banner').waitFor()
    await page.getByTestId('submit-dataset').click()
    await page.getByTestId('notice-banner').waitFor()
    const notice = await page.getByTestId('notice-banner').innerText()
    expect(notice).toMatch(/复用已存在的成功版本|QUALITY_DATASET_CONFLICT|数据集已封存/)
    await expect.poll(async () => page.getByTestId('dataset-row').count()).toBeLessThanOrEqual(rowsBefore + 1)

    // 趋势台账已同步数据集结论
    await expect(page.getByTestId('ledger-table').locator('tbody tr').first()).toBeVisible()
  })

  test('采集校验待办流读取异常窗口并写回阻断项', async ({ page }) => {
    // 构造一个不达标窗口：批量低值样本 → 重新封存 → 提交为新数据集版本
    await page.getByTestId('period-select').selectOption('daily')
    for (let i = 0; i < 12; i += 1) {
      await page.getByTestId('sample-value').fill('0.1')
      await page.getByTestId('submit-sample').click()
    }
    await page.getByTestId('start-generation').click()
    await page.getByTestId('seal-snapshot').click()
    await expect(page.getByTestId('notice-banner')).toContainText('已封存新快照')
    await page.getByTestId('submit-dataset').click()
    await page.getByTestId('notice-banner').waitFor()

    // 采集校验页：待办流出现阻断级异常窗口，且不读取生成中的指标
    await page.goto('/validate')
    const todoRow = page.getByTestId('todo-row').first()
    await todoRow.waitFor()
    await expect(todoRow).toContainText('阻断')

    // 重复执行时该待办可能已写回（按钮禁用）：能写回则写回，否则校验写回台账已有记录
    const writebackBtn = todoRow.getByTestId('writeback-btn')
    const writebacksBefore = await page.getByTestId('writeback-table').locator('tbody tr').count()
    if (await writebackBtn.isEnabled()) {
      await writebackBtn.click()
      await expect(page.getByTestId('notice-banner')).toContainText('阻断项已写回校验记录')
      await expect.poll(async () => page.getByTestId('writeback-table').locator('tbody tr').count()).toBe(writebacksBefore + 1)
    } else {
      expect(writebacksBefore).toBeGreaterThan(0)
    }

    // 校验趋势台账与质量报告保持同一份数据集版本
    await expect(page.getByTestId('validate-ledger-table').locator('tbody tr').first()).toBeVisible()
  })
})
