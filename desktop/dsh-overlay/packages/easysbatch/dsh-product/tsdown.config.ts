import { clientBundle } from '../../client/tsdown.client.ts'

export default clientBundle(
  '@deepseek-ai/dsh-easysbatch-product',
  ['lib/types/index.js'],
  { hostPhase: true },
)
