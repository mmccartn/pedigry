import MiniSearch from 'minisearch'
import { useEffect, useMemo, useState } from 'react'
import PedigreeGrid from './components/PedigreeGrid'
import { displayName, type Dataset } from './types'

type SearchDocument = {
  id: string
  name: string
  titles: string
  registrationNumber: string
  source: string
}

export default function App() {
  const [dataset, setDataset] = useState<Dataset | null>(null)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tableFocusId, setTableFocusId] = useState<string | null>(null)
  const [tableTrail, setTableTrail] = useState<string[]>([])
  const [tableDepth, setTableDepth] = useState(6)

  useEffect(() => {
    fetch(`${import.meta.env.BASE_URL}data/pedigree_dataset.json`)
      .then((response) => {
        if (!response.ok) throw new Error(`Dataset request failed: ${response.status}`)
        return response.json()
      })
      .then((data: Dataset) => {
        setDataset(data)
        const root = Object.entries(data.nodes).find(([, node]) => node.name === "Riviera's Some Like It Hot")
        const hashId = decodeURIComponent(window.location.hash.replace(/^#dog\//, ''))
        const initialId = data.nodes[hashId] ? hashId : root?.[0] ?? Object.keys(data.nodes)[0] ?? null
        setSelectedId(initialId)
        setTableFocusId(initialId)
        setTableTrail(initialId ? [initialId] : [])
        if (initialId && !hashId) window.history.replaceState({ dogId: initialId }, '', `#dog/${encodeURIComponent(initialId)}`)
      })
      .catch((error: unknown) => console.error(error))
  }, [])

  const search = useMemo(() => {
    if (!dataset) return null
    const index = new MiniSearch<SearchDocument>({
      fields: ['name', 'titles', 'registrationNumber'],
      storeFields: ['name', 'titles', 'registrationNumber', 'source'],
      searchOptions: { boost: { registrationNumber: 6, name: 4 }, fuzzy: 0.2, prefix: true },
    })
    index.addAll(Object.entries(dataset.nodes).map(([id, node]) => ({
      id,
      name: node.name ?? '',
      titles: node.titles.join(' '),
      registrationNumber: node.registration_number ?? '',
      source: node.primary_source,
    })))
    return index
  }, [dataset])

  const results = useMemo(() => {
    const term = query.trim()
    if (!search || term.length < 3) return []
    return search.search(term, { combineWith: 'AND', prefix: true, fuzzy: 0.2 }).slice(0, 25)
  }, [query, search])

  const highlightedIds = useMemo(() => new Set(results.map((result) => result.id)), [results])

  const selectDog = (id: string, pushHistory = true) => {
    if (!dataset?.nodes[id]) return
    setSelectedId(id)
    setTableFocusId(id)
    setTableTrail([id])
    if (pushHistory) window.history.pushState({ dogId: id }, '', `#dog/${encodeURIComponent(id)}`)
  }

  const pageTableToDog = (id: string, lineage: string[]) => {
    if (!dataset?.nodes[id]) return
    setTableFocusId(id)
    setTableTrail(lineage)
    window.history.pushState({ dogId: selectedId, tableFocusId: id, tableTrail: lineage }, '', `#dog/${encodeURIComponent(selectedId ?? id)}`)
  }

  useEffect(() => {
    const restoreDog = () => {
      const state = window.history.state
      const id = state?.dogId ?? decodeURIComponent(window.location.hash.replace(/^#dog\//, ''))
      if (!dataset?.nodes[id]) return
      setSelectedId(id)
      const pageId = state?.tableFocusId
      setTableFocusId(dataset.nodes[pageId] ? pageId : id)
      setTableTrail(Array.isArray(state?.tableTrail) ? state.tableTrail : [id])
    }
    window.addEventListener('popstate', restoreDog)
    return () => window.removeEventListener('popstate', restoreDog)
  }, [dataset])

  if (!dataset || !selectedId) return <main className="loading">Loading pedigry dataset…</main>

  const selected = dataset.nodes[selectedId]

  return (
    <main>
      <header className="workspace-bar">
        <div className="brand">
          <span>pedigry</span>
          <strong>Root: {displayName(selected)}</strong>
          <small>{selected.registration_number || selected.primary_source}</small>
        </div>
        <section className="search-panel">
          <label htmlFor="dog-search">Find a dog</label>
          <input id="dog-search" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && results[0]) { selectDog(results[0].id); setQuery('') } }} placeholder="Type 3+ characters to highlight matches" />
          {results.length > 0 && (
            <div className="results">
              {results.map((result) => (
                <button key={result.id} onClick={() => { selectDog(result.id); setQuery('') }}>
                  <strong>{[result.titles, result.name].filter(Boolean).join(' ')}</strong>
                  <span>{result.registrationNumber || result.source}</span>
                </button>
              ))}
            </div>
          )}
        </section>
      </header>

      <PedigreeGrid focusId={tableFocusId ?? selectedId} rootId={selectedId} nodes={dataset.nodes} highlightedIds={highlightedIds} depth={tableDepth} rootTrail={tableTrail} onDepthChange={setTableDepth} onPage={pageTableToDog} onSelect={selectDog} />
    </main>
  )
}
