// -*- coding: utf-8 -*-
/**
 * Node 用例：验证 dsh-wins 适配文件能在 DSH 会话目录里标记本地变更。
 * 运行：node --test tests/dsh_wins.test.mjs
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { applyDshWins } from '../scripts/dsh_wins_adapter.js'

function makeSession(cards, facts, journal) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'jinyv-dsh-wins-'))
  fs.writeFileSync(path.join(directory, 'cards.json'),
    `${JSON.stringify(cards, null, 2)}\n`, 'utf8')
  fs.writeFileSync(path.join(directory, 'facts.json'),
    `${JSON.stringify(facts, null, 2)}\n`, 'utf8')
  fs.writeFileSync(path.join(directory, '.jinyv-bridge.json'),
    `${JSON.stringify(journal, null, 2)}\n`, 'utf8')
  return directory
}

test('dsh-wins 标记双端变更中的 DSH 侧文本', () => {
  const directory = makeSession(
    [{
      id: 'core-core-1',
      summary: 'DSH 侧新文本',
      hooks: ['冲突'],
      relHooks: [],
      importance: 0.7,
      boundary: 'both',
      turnStart: 9,
      turnEnd: 9,
      jinyv_core_id: 'core-1',
    }],
    [{
      id: 'core-F1',
      text: 'DSH 侧新事实',
      tags: ['冲突'],
      kind: '事件',
      importance: 0.8,
      turn: 9,
      jinyv_core_id: '1',
    }],
    {
      version: 1,
      imported: {},
      exported: {
        'card:core-1': {
          dsh_id: 'core-core-1',
          core_sig: 'old-core',
          dsh_sig: 'old-dsh',
        },
        'fact:1': {
          dsh_id: 'core-F1',
          core_sig: 'old-core',
          dsh_sig: 'old-dsh',
        },
      },
      conflicts: [],
      dsh_claims: [],
    },
  )
  try {
    const first = applyDshWins(directory)
    assert.equal(first.claims, 2)
    assert.equal(first.policy, 'dsh-wins')
    const journal = JSON.parse(fs.readFileSync(
      path.join(directory, '.jinyv-bridge.json'), 'utf8'))
    assert.equal(journal.dsh_claims.length, 2)
    assert.equal(journal.dsh_policy, 'dsh-wins')
    const cards = JSON.parse(fs.readFileSync(path.join(directory, 'cards.json'), 'utf8'))
    assert.equal(cards[0].jinyv_authoritative, true)
    assert.equal(cards[0].jinyv_policy, 'dsh-wins')
    const facts = JSON.parse(fs.readFileSync(path.join(directory, 'facts.json'), 'utf8'))
    assert.equal(facts[0].jinyv_authoritative, true)
    const config = JSON.parse(fs.readFileSync(path.join(directory, 'config.json'), 'utf8'))
    assert.equal(config.jinyvConflictPolicy, 'dsh-wins')

    const second = applyDshWins(directory)
    assert.equal(second.claims, 0)
    const journalAgain = JSON.parse(fs.readFileSync(
      path.join(directory, '.jinyv-bridge.json'), 'utf8'))
    assert.equal(journalAgain.dsh_claims.length, 2)
  } finally {
    fs.rmSync(directory, { recursive: true, force: true })
  }
})
