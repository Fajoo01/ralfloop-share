/* Runs only inside the authenticated ChatGPT page. No credentials leave it. */
(async () => {
  const input = __CATALOG_INPUT__;
  const fail = code => { throw new Error(code); };
  const root = document.documentElement;
  let fiber = root[Object.keys(root).find(k => k.startsWith('__reactFiber$'))];
  if (!fiber) return JSON.stringify({error: 'account_catalog_source_unavailable'});
  while (fiber.return) fiber = fiber.return;
  const stack = [fiber], seen = new Set();
  let client;
  while (stack.length && seen.size < 25000) {
    const node = stack.pop();
    if (!node || seen.has(node)) continue;
    seen.add(node);
    for (const value of Object.values(node.memoizedProps || {})) {
      if (value && typeof value.getQueryCache === 'function') client = value;
    }
    stack.push(node.child, node.sibling);
  }
  if (!client) return JSON.stringify({error: 'account_catalog_source_unavailable'});
  const memo = {pages: new Map()};
  const queries = client.getQueryCache().getAll();
  const projectQuery = queries.find(q => q.queryKey[0] === 'snorlax-history');
  const canonicalProject = value => String(value || '').replace(/^(g-p-[a-f0-9]+)(?:-.*)?$/, '$1');
  const projectId = canonicalProject(input.project_id);
  const scope = JSON.stringify([projectId, input.query]);
  if (input.cursor && JSON.parse(input.cursor).scope !== scope) return JSON.stringify({error: 'account_catalog_cursor_scope_mismatch'});
  const historyQuery = projectId && projectId !== '__none__'
    ? queries.find(q => q.queryKey[0] === 'snorlaxConversations' && q.queryKey[1]?.gizmoId === projectId && q.queryKey.length === 2)
      || queries.find(q => q.queryKey[0] === 'snorlaxConversations' && q.queryKey[1]?.gizmoId === projectId)
    : queries.find(q => q.queryKey[0] === 'conversationHistory' && (input.project_id !== '__none__' ? q.queryKey.length === 1 : q.queryKey[1]?.hideProjectChats));
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 16000);
  const fetchPage = async (q, param) => {
    if (!q || typeof q.options.queryFn !== 'function') fail('account_catalog_query_unavailable');
    const cacheKey = JSON.stringify([q.queryKey, param ?? null]);
    if (memo.pages.has(cacheKey)) return memo.pages.get(cacheKey);
    const cached = q.state.data;
    const index = cached?.pageParams?.findIndex(p => JSON.stringify(p ?? null) === JSON.stringify(param ?? null));
    if (index >= 0 && cached.pages[index]) return cached.pages[index];
    try {
      const page = await q.options.queryFn({client, queryKey: q.queryKey, pageParam: param,
        signal: controller.signal, direction: 'forward', meta: q.options.meta});
      memo.pages.set(cacheKey, page);
      return page;
    } catch (e) {
      const status = e?.status || e?.response?.status;
      fail(status === 429 ? 'account_catalog_rate_limited' : 'account_catalog_fetch_failed');
    }
  };
  const nextParam = (q, page, pages, param) => q.options.getNextPageParam?.(page, pages, param, []) ?? null;
  const projects = new Map(), chats = new Map();
  let projectsComplete = false, warning = '', next = null;
  try {
    if (projectQuery && !projectId) {
      let param = projectQuery.options.initialPageParam, pages = [];
      for (let count = 0; count < 20; count++) {
        const page = await fetchPage(projectQuery, param);
        if (!Array.isArray(page.items)) fail('account_catalog_schema_changed');
        pages.push(page);
        for (const item of page.items) {
          const g = item.gizmo?.gizmo;
          if (!g || !/^g-p-[a-zA-Z0-9_-]+$/.test(g.id)) continue;
          const slug = /^g-p-[a-zA-Z0-9_-]+$/.test(g.short_url || '') ? g.short_url : g.id;
          projects.set(g.id, {project_id: g.id, title: g.display?.name || 'Progetto ChatGPT', url: `https://chatgpt.com/g/${slug}/project`});
        }
        const following = nextParam(projectQuery, page, pages, param);
        if (following === null) { projectsComplete = true; break; }
        if (JSON.stringify(following) === JSON.stringify(param)) fail('account_catalog_cursor_stalled');
        param = following;
      }
    }
    if (!projectId && !projectQuery) warning = 'account_catalog_projects_unavailable';
    else if (!projectId && !projectsComplete) warning = 'account_catalog_projects_incomplete';
    if (!historyQuery) fail(projectId ? 'account_catalog_open_project_first' : 'account_catalog_query_unavailable');
    let param = input.cursor ? JSON.parse(input.cursor).page : historyQuery.options.initialPageParam;
    let pages = [];
    for (let count = 0; count < (input.query ? 5 : 1); count++) {
      const page = await fetchPage(historyQuery, param);
      if (!Array.isArray(page.items)) fail('account_catalog_schema_changed');
      if (!page.items.length && document.querySelector('#modal-conversation-history-rate-limit') && !projectId) fail('account_catalog_rate_limited');
      pages.push(page);
      for (const item of page.items) {
        if (!/^[a-zA-Z0-9-]+$/.test(item.id || '')) continue;
        const pid = /^g-p-[a-zA-Z0-9_-]+$/.test(item.gizmo_id || '') ? canonicalProject(item.gizmo_id) : (projectId !== '__none__' ? projectId : '');
        if (input.project_id === '__none__' && pid) continue;
        if (input.query && !String(item.title || '').toLocaleLowerCase().includes(input.query.toLocaleLowerCase())) continue;
        const project = projects.get(pid);
        const slug = project ? new URL(project.url).pathname.split('/')[2] : pid;
        const url = `https://chatgpt.com/c/${item.id}`;
        chats.set(item.id, {url, context_url: slug ? `https://chatgpt.com/g/${slug}/c/${item.id}` : url,
          title: item.title || 'Chat GPT', project_id: pid});
      }
      next = nextParam(historyQuery, page, pages, param);
      if (next === null || chats.size >= input.limit) break;
      if (JSON.stringify(next) === JSON.stringify(param)) fail('account_catalog_cursor_stalled');
      param = next;
    }
  } catch (e) {
    warning = /^account_catalog_[a-z_]+$/.test(e.message || '') ? e.message : 'account_catalog_fetch_failed';
  } finally { clearTimeout(timeout); }
  return JSON.stringify({chats: [...chats.values()], projects: [...projects.values()],
    source: 'chatgpt_query_client', search_scope: 'title', projects_complete: projectsComplete,
    complete: !warning && next === null, next_cursor: next === null ? '' : JSON.stringify({page: next, scope}), warning});
})()
