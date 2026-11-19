// Koishi 启动入口：读同目录 koishi.yml，按依赖顺序注册插件。
// 不用 koishi CLI 的插件名解析——@satorijs/plugin-server 和 Koishi 自带的
// @koishijs/plugin-server 同名，CLI 会解析到后者。
const fs = require('fs')
const path = require('path')
const yaml = require('js-yaml')
const { Context } = require('koishi')

// require 出来的包可能是函数、default 导出或 { apply } 对象，统一取 apply
function load(name) {
  const mod = require(name)
  if (typeof mod === 'function') return mod
  if (mod && typeof mod.default === 'function') return mod.default
  if (mod && typeof mod.apply === 'function') return mod
  if (mod && mod.default && typeof mod.default.apply === 'function') return mod.default
  throw new Error(`插件 ${name} 的导出无法识别`)
}

const cfg = yaml.load(fs.readFileSync(path.join(__dirname, 'koishi.yml'), 'utf8'))
const plugins = cfg.plugins || {}
const app = new Context({ prefix: cfg.prefix || ['.'] })

app.on('internal/error', (err) => console.error('[boot] internal error:', err))

// 顺序不能换：http -> 监听 -> KOOK 适配器 -> Satori 协议层
app.plugin(load('@koishijs/plugin-http'), {})
app.plugin(load('@koishijs/plugin-server'), plugins['@koishijs/plugin-server'])
app.plugin(load('@koishijs/plugin-adapter-kook'), plugins['adapter-kook'])
app.plugin(load('@satorijs/plugin-server'), plugins['@satorijs/plugin-server'])

app.on('ready', () => console.log('[boot] koishi ready, bots =', app.bots && app.bots.length))
app.start().catch((err) => {
  console.error('[boot] 启动失败:', err)
  process.exit(1)
})
