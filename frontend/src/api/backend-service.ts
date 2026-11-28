import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export async function fetchSnapshot(key: string): Promise<Record<string, any>> {
  const response = await request(`/api/snapshot/${key}`)
  if (!response.ok) throw new Error('跨模块快照读取失败')
  return await response.json()
}

export type SupplementCandidate = {
  id: number
  attempt_id: string
  '航次编号': string
  '数据内容': string
  '目标系统': string
  '轮次': number
  retry_eligible: boolean
  claimed: boolean
  '领取回执号'?: string | null
  '已有进行中轮次'?: boolean
  [field: string]: unknown
}

export type SupplementSummary = {
  items: SupplementCandidate[]
  total: number
  by_target: Record<string, number>
}

export type SupplementInfo = {
  report_id: number
  '航次编号': string
  '补件状态'?: string
  credential: Record<string, any> | null
  receipts: Record<string, any>[]
  '最近失败传输': Record<string, any> | null
  '科研归档接收'?: Record<string, any> | null
}

export async function listSupplementCandidates(
  params: Record<string, string> = {},
): Promise<SupplementSummary> {
  const query = new URLSearchParams(params).toString()
  const response = await request(`/api/data_transfer/candidates?${query}`)
  if (!response.ok) throw new Error('补传候选读取失败')
  return await response.json()
}

export async function claimSupplement(
  id: number,
  values: Record<string, unknown> = {},
): Promise<ActionResult> {
  const response = await request(`/api/data_transfer/${id}/claim`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function replayLateConfirmation(
  id: number,
  values: Record<string, unknown> = {},
): Promise<ActionResult> {
  const response = await request(`/api/data_transfer/${id}/late-confirmation`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function fetchSupplementInfo(reportId: number): Promise<SupplementInfo> {
  const response = await request(`/api/results_compile/${reportId}/supplement`)
  if (!response.ok) throw new Error('汇编报告补件信息读取失败')
  return await response.json()
}
