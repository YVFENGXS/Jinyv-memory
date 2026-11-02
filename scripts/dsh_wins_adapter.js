// -*- coding: utf-8 -*-
/**
 * jinyv-dsh-wins：DSH 插件侧的 dsh-wins 适配文件。
 *
 * 这个文件不是第三方源码，也不复制 jinyu-memory-dsh 的实现；安装脚本把它放进
 * 插件 lib/ 并在 index.js 里注入两行（import + installDshWins(ctx)）。
 *
 * 职责：
 * 1. 在会话配置里声明 jinyvConflictPolicy = "dsh-wins"；
 * 2. 当本地卡片/事实相对上次桥接 journal 发生变化时，标记 jinyv_authoritative，
 *    并在 .jinyv-bridge.json 写入 dsh_claims，供核心桥在双端冲突时采用 DSH 文本；
 * 3. 不改写插件原有记忆逻辑，不改核心库。
 */
import fs from 'node:fs'
import path from 'node:path'
import { createHash } from 'node:crypto'

const JOURNAL = '.jinyv-bridge.json'
const POLICY = 'dsh-wins'
const MAX_CLAIMS = 200

function readJson(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return fallback
  }
}

function writeJsonAtomic(file, value) {
  const tmp = `${file}.tmp-${process.pid}-${Date.now()}`
  fs.writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
  fs.renameSync(tmp, file)
}

function round3(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return 0.5
  return Math.round(Math.max(0, Math.min(1, number)) * 1000) / 1000
}

function integer(value, fallback = 0) {
  const number = Number(value)
  return Number.isFinite(number) ? Math.max(0, Math.floor(number)) : fallback
}

function strings(value) {
  return Array.isArray(value) ? value.map(item => String(item)) : []
}

function normCard(card) {
  return {
    summary: String(card?.summary ?? '').trim(),
    hooks: strings(card?.hooks ?? card?.entity_hooks),
    rel_hooks: strings(card?.relHooks ?? card?.rel_hooks),
    importance: round3(card?.importance ?? 0.5),
    boundary: String(card?.boundary ?? card?.knowledge_boundary ?? 'both'),
    turn_start: integer(card?.turnStart ?? card?.turn_start),
    turn_end: integer(card?.turnEnd ?? card?.turn_end),
    mentions: integer(card?.mentions),
    verified: integer(card?.verified),
  }
}

function normFact(fact) {
  return {
    text: String(fact?.text ?? fact?.fact ?? '').trim(),
    tags: strings(fact?.tags),
    kind: String(fact?.kind ?? fact?.category ?? ''),
    importance: round3(fact?.importance ?? 0.5),
    turn: integer(fact?.turn ?? fact?.turn_end),
  }
}

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort()
      .map(key => `${JSON.stringify(key)}:${stable(value[key])}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}

function digest(value) {
  return createHash('sha256').update(stable(value)).digest('hex')
}

function cardSig(card) {
  return digest(normCard(card))
}

function factSig(fact) {
  return digest(normFact(fact))
}

/**
 * 标记 DSH 侧相对 journal 发生变化的条目，并写入 dsh-wins 声明。
 * @param {string} storeDir - DSH session 目录。
 * @param {object} [logger] - 可选 logger。
 * @returns {{claims: number, policy: string}} 新写入的声明数量。
 */
export function applyDshWins(storeDir, logger) {
  const directory = String(storeDir || '')
  if (!directory) return { claims: 0, policy: POLICY }
  const journalFile = path.join(directory, JOURNAL)
  const journal = readJson(journalFile, null)
  if (!journal || typeof journal !== 'object') {
    return { claims: 0, policy: POLICY }
  }
  const cardsFile = path.join(directory, 'cards.json')
  const factsFile = path.join(directory, 'facts.json')
  const cards = readJson(cardsFile, [])
  const facts = readJson(factsFile, [])
  const exported = journal.exported && typeof journal.exported === 'object'
    ? journal.exported
    : {}
  const existingClaims = new Set((journal.dsh_claims || [])
    .map(item => `${item.kind}:${item.id}:${item.dsh_sig}`))
  const claims = []
  const now = new Date().toISOString()
  let changedCards = false
  let changedFacts = false

  const claimOf = (kind, item, coreId, signature, lastSynced) => {
    const key = `${kind}:${String(item.id || '')}:${signature}`
    item.jinyv_authoritative = true
    item.jinyv_policy = POLICY
    item.jinyv_changed_at = now
    const entry = exported[kind === 'card' ? `card:${coreId}` : `fact:${coreId}`]
    if (entry) entry.dsh_changed_sig = signature
    if (existingClaims.has(key)) return
    existingClaims.add(key)
    claims.push({
      kind,
      id: String(item.id || ''),
      core_id: String(coreId || ''),
      dsh_sig: signature,
      last_synced_sig: String(lastSynced || ''),
      policy: POLICY,
      at: now,
    })
  }

  for (const [key, entry] of Object.entries(exported)) {
    if (!entry || typeof entry !== 'object') continue
    if (key.startsWith('card:')) {
      const coreId = key.slice(5)
      const target = Array.isArray(cards)
        ? cards.find(item => String(item.id || '') === String(entry.dsh_id || ''))
          || cards.find(item => String(item.jinyv_core_id || '') === coreId)
        : undefined
      if (!target) continue
      const signature = cardSig(target)
      if (signature === entry.dsh_sig) continue
      claimOf('card', target, coreId, signature, entry.dsh_sig)
      changedCards = true
    } else if (key.startsWith('fact:')) {
      const coreId = key.slice(5)
      const target = Array.isArray(facts)
        ? facts.find(item => String(item.id || '') === String(entry.dsh_id || ''))
          || facts.find(item => String(item.jinyv_core_id || '') === coreId)
        : undefined
      if (!target) continue
      const signature = factSig(target)
      if (signature === entry.dsh_sig) continue
      claimOf('fact', target, coreId, signature, entry.dsh_sig)
      changedFacts = true
    }
  }

  if (claims.length > 0) {
    journal.dsh_claims = [...(journal.dsh_claims || []), ...claims].slice(-MAX_CLAIMS)
    journal.dsh_policy = POLICY
    journal.dsh_policy_updated_at = now
    writeJsonAtomic(journalFile, journal)
    if (changedCards) writeJsonAtomic(cardsFile, cards)
    if (changedFacts) writeJsonAtomic(factsFile, facts)
    logger?.info?.(`jinyv-dsh-wins: 标记 ${claims.length} 条 DSH 侧变更`)
  }

  const configFile = path.join(directory, 'config.json')
  const config = readJson(configFile, {}) || {}
  if (config.jinyvConflictPolicy !== POLICY) {
    config.jinyvConflictPolicy = POLICY
    config.jinyvConflictPolicySource = 'jinyv-dsh-wins'
    config.jinyvConflictPolicyUpdatedAt = now
    writeJsonAtomic(configFile, config)
  }
  return { claims: claims.length, policy: POLICY }
}

/**
 * 注入到 jinyu-memory-dsh 的 apply(ctx)。安装脚本只会调用一次。
 * @param {object} ctx - DSH plugin context。
 */
export function installDshWins(ctx, createStore) {
  if (!ctx || ctx.__jinyvDshWinsInstalled) return
  if (typeof createStore !== 'function') {
    ctx.logger?.warn?.('jinyv-dsh-wins: 缺少 createStore，跳过安装')
    return
  }
  ctx.__jinyvDshWinsInstalled = true
  const stores = new Map()

  function storeFor(session) {
    let store = stores.get(session.id)
    if (!store) {
      store = createStore({ sessionId: session.id })
      stores.set(session.id, store)
    }
    return store
  }

  ctx.on?.('session/event', (session, event) => {
    if (!session || !event) return
    if (event.type !== 'turn/start' && event.type !== 'turn/end') return
    const store = storeFor(session)
    setTimeout(() => {
      try {
        applyDshWins(store.dir, ctx.logger)
      } catch (error) {
        ctx.logger?.warn?.(`jinyv-dsh-wins: ${error?.message || error}`)
      }
    }, 0)
  })

  ctx.on?.('session/disposed', session => {
    if (session) stores.delete(session.id)
  })

  ctx.logger?.info?.('jinyv-dsh-wins: dsh-wins 策略已安装')
}
