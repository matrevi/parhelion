// Minimal node check for the DSH Parhelion mount plugin (4.2 G / 4.1 #15).
//
// The plugin normally runs inside DeepSeek Harness, so this script fabricates the plugin context
// (agents, loader, effect, on, logger) and a contest workspace on disk, calls `apply()` for real,
// and prints the JSON that the Python test asserts. It never writes to ~/.dsh.
import { mkdirSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const pluginUrl = new URL('../../opsb_core/drivers/dsh_parhelion_mcp.mjs', import.meta.url)
const plugin = await import(pluginUrl.href)

const root = mkdtempSync(join(tmpdir(), 'p08-plugin-'))
const worktree = join(root, '.worktrees', 'sprint-solo')
mkdirSync(worktree, { recursive: true })
mkdirSync(join(root, 'SgemmFp16Cube'), { recursive: true })
writeFileSync(join(root, 'opsb.workspace.json'), '{"problems":[]}\n')

const events = {}
const mounts = []
function fakeAgent(id, cwd, header = {}) {
  return {
    id,
    session: { header: { cwd, ...header } },
    ctx: {
      plugin(client, config) {
        mounts.push({ agent: id, serverName: config.serverName, args: config.args, cwd: config.cwd,
                      roleArg: config.args[config.args.indexOf('--role') + 1] })
        const fiber = Promise.resolve()
        fiber.dispose = async () => {}
        return fiber
      },
      effect() {}
    }
  }
}

const agents = [
  fakeAgent('main', root),
  fakeAgent('worktree-worker', worktree),
  fakeAgent('native-child', root, { origin: 'subagent', parentSession: 'main', delegationDepth: 1 }),
  fakeAgent('child-in-worktree', worktree, { origin: 'subagent', parentSession: 'main' }),
  fakeAgent('unclassified', join(root, 'SgemmFp16Cube')),
  fakeAgent('elsewhere', join(tmpdir(), 'not-a-contest'))
]

const ctx = {
  effect() {},
  on(name, handler) { events[name] = handler },
  logger: { warn: () => {}, info: () => {}, error: () => {} },
  loader: { import: async () => (() => {}) },
  agents: { list: () => agents }
}

await plugin.apply(ctx, { opsb: '/tmp/fake-opsb', command: 'python3' })

const cases = agents.map(agent => {
  const plan = plugin.planMount(agent, { opsb: '/tmp/fake-opsb' })
  return { id: agent.id, root: plan.root, role: plan.role, mounted: mounts.some(m => m.agent === agent.id) }
})

const out = {
  cases,
  mounts: mounts.map(m => ({ agent: m.agent, serverName: m.serverName, cwd: m.cwd, role: m.roleArg,
                             args: m.args })),
  event: Object.keys(events)
}
process.stdout.write(JSON.stringify(out))
rmSync(root, { recursive: true, force: true })
