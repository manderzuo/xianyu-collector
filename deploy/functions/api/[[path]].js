export async function onRequest(context) {
  const backend = context.env.BACKEND_URL
  if (!backend) return new Response('BACKEND_URL is not configured', { status: 503 })
  const incoming = new URL(context.request.url)
  const target = new URL(incoming.pathname + incoming.search, backend.replace(/\/$/, '') + '/')
  return fetch(new Request(target, context.request))
}
