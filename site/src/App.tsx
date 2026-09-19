import MiniSearch from 'minisearch'
import { useEffect, useMemo, useState } from 'react'
import PedigreeGraph from './components/PedigreeGraph'
import { displayName, type Dataset } from './types'

type SearchDocument = {
  id: string
  name: string
  titles: string
  registrationNumber: string
  source: string
}

function ParentCard({ id, label, nodes, onSelect }: { id: string | null; label: string; nodes: Dataset['nodes']; onSelect: (id: string) => void }) {
  const node = id ? nodes[id] : undefined
  return (
    <button className="parent-card" disabled={!node} onClick={() => node && onSelect(id!)}>
      <span>{label}</span>
      <strong>{node ? displayName(node) : 'Unknown'}</strong>
      {node?.registration_number && <small>{node.registration_number}</small>}
    </button>
  )
}

export default function App() {
  const [dataset, setDataset] = useState<Dataset | null>(null)
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [depth, setDepth] = useState(4)

  useEffect(() => {
    fetch('/data/pedigree_dataset.json')
      .then((response) => {
        if (!response.ok) throw new Error(`Dataset request failed: ${response.status}`)
        return response.json()
      })
      .then((data: Dataset) => {
        setDataset(data)
        const root = Object.entries(data.nodes).find(([, node]) => node.name === "Riviera's Some Like It Hot")
        setSelectedId(root?.[0] ?? Object.keys(data.nodes)[0] ?? null)
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
    if (!search || !query.trim()) return []
    return search.search(query, { combineWith: 'AND' }).slice(0, 10)
  }, [query, search])

  if (!dataset || !selectedId) return <main className="loading">Loading pedigry dataset…</main>

  const selected = dataset.nodes[selectedId]
  const connected = [selected.sire, selected.dam].filter(Boolean).length

  return (
    <main>
      <header>
        <div>
          <p className="eyebrow">Pedigree Explorer</p>
          <h1>pedigry</h1>
        </div>
        <div className="stats">
          <span>{dataset.stats.total_nodes.toLocaleString()} dogs</span>
          <span>{dataset.stats.dobequest_only_nodes.toLocaleString()} DobeQuest-only</span>
        </div>
      </header>

      <section className="search-panel">
        <label htmlFor="dog-search">Find a dog</label>
        <input id="dog-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Name, title, or registration number" />
        {results.length > 0 && (
          <div className="results">
            {results.map((result) => (
              <button key={result.id} onClick={() => { setSelectedId(result.id); setQuery('') }}>
                <strong>{[result.titles, result.name].filter(Boolean).join(' ')}</strong>
                <span>{result.registrationNumber || result.source}</span>
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="profile-grid">
        <aside className="pedigree-column">
          <ParentCard label="Sire" id={selected.sire} nodes={dataset.nodes} onSelect={setSelectedId} />
          <div className="line" />
          <ParentCard label="Dam" id={selected.dam} nodes={dataset.nodes} onSelect={setSelectedId} />
        </aside>

        <article className="dog-card">
          <p className="eyebrow">Focused dog</p>
          <h2>{displayName(selected)}</h2>
          <dl>
            <div><dt>Registration</dt><dd>{selected.registration_number || 'Unavailable'}</dd></div>
            <div><dt>Born</dt><dd>{selected.birth_date || 'Unavailable'}</dd></div>
            <div><dt>Sex</dt><dd>{selected.sex || 'Unavailable'}</dd></div>
            <div><dt>Source</dt><dd>{selected.primary_source === 'akc' ? 'AKC' : 'DobeQuest'}</dd></div>
            <div><dt>Known parents</dt><dd>{connected} / 2</dd></div>
          </dl>
        </article>
      </section>

      <section className="depth-controls">
        <div>
          <p className="eyebrow">Ancestry depth</p>
          <h2>Explore ancestors</h2>
        </div>
        <label className="depth-slider">
          <span>{depth === 60 ? 'All known generations' : `${depth} generations`}</span>
          <input type="range" min="2" max="60" value={depth} onChange={(event) => setDepth(Number(event.target.value))} />
        </label>
      </section>

      <PedigreeGraph focusId={selectedId} nodes={dataset.nodes} depth={depth} onSelect={setSelectedId} />
    </main>
  )
}
