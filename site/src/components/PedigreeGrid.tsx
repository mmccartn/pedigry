import * as d3 from 'd3'
import { useEffect, useMemo, useRef, useState } from 'react'
import { type DogNode } from '../types'

type Cell = {
  id: string
  canonicalId: string
  dog: DogNode
  depth: number
  top: number
  bottom: number
  state: 'expanded' | 'loop'
  lineage: string[]
  children: Array<Cell | null>
}

type Props = {
  focusId: string
  rootId: string
  nodes: Record<string, DogNode>
  highlightedIds: Set<string>
  depth: number
  rootTrail: string[]
  onDepthChange: (depth: number) => void
  onPage: (id: string, lineage: string[]) => void
  onSelect: (id: string) => void
}

const maxCells = 16_000
const cellWidth = 190
const rowHeight = 38
const textBlockHeight = 32

function buildTable(focusId: string, nodes: Record<string, DogNode>, maxDepth: number) {
  let count = 0

  const visit = (
    id: string | null,
    depth: number,
    path: Set<string>,
    lineage: string[],
  ): Cell | null => {
    const dog = id ? nodes[id] : undefined
    if (!dog || !id || count++ >= maxCells) return null

    const loop = path.has(id)
    const cell: Cell = {
      id: `${id}:${count}`,
      canonicalId: id,
      dog,
      depth,
      top: 0,
      bottom: 0,
      state: loop ? 'loop' : 'expanded',
      lineage: [...lineage, id],
      children: [],
    }

    if (loop || depth === maxDepth) return cell

    const nextPath = new Set(path).add(id)
    for (const parent of [dog.sire, dog.dam]) {
      cell.children.push(visit(parent, depth + 1, nextPath, cell.lineage))
    }

    return cell
  }

  const root = visit(focusId, 0, new Set(), [])
  if (!root) throw new Error(`Missing focused dog: ${focusId}`)

  let leaf = 0
  const place = (cell: Cell): void => {
    if (!cell.children.some(Boolean)) {
      cell.top = leaf * rowHeight
      cell.bottom = cell.top + rowHeight
      leaf += 1
      return
    }

    const start = leaf
    for (const child of cell.children) {
      if (child) place(child)
      else leaf += 1
    }
    cell.top = start * rowHeight
    cell.bottom = leaf * rowHeight
  }
  place(root)

  const cells: Cell[] = []
  const collect = (cell: Cell) => {
    cells.push(cell)
    cell.children.forEach((child) => child && collect(child))
  }
  collect(root)

  return { root, cells, height: leaf * rowHeight }
}

function cellClass(cell: Cell, highlighted: Set<string>) {
  const classes = [
    'pedigree-cell',
    cell.dog.sex === 'Male' ? 'male' : cell.dog.sex === 'Female' ? 'female' : 'unknown',
  ]

  if (cell.state === 'loop') classes.push('loop')
  if (highlighted.has(cell.canonicalId)) classes.push('search-match')

  return classes.join(' ')
}

export default function PedigreeGrid({
  focusId,
  rootId,
  nodes,
  highlightedIds,
  depth,
  rootTrail,
  onDepthChange,
  onPage,
  onSelect,
}: Props) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const activeLineageRef = useRef<HTMLButtonElement>(null)
  const zoomRef = useRef<d3.ZoomBehavior<HTMLDivElement, unknown> | null>(null)
  const selectionRef = useRef<d3.Selection<HTMLDivElement, unknown, null, undefined> | null>(null)
  const [transform, setTransform] = useState(d3.zoomIdentity)
  const [size, setSize] = useState({ width: 1, height: 1 })

  const table = useMemo(() => buildTable(focusId, nodes, depth), [depth, focusId, nodes])
  const width = (depth + 1) * cellWidth

  const focusRoot = () => {
    const viewport = viewportRef.current
    if (!viewport) return

    const scale = Math.min(
      1,
      (viewport.clientWidth - 30) / (cellWidth * 5),
      (viewport.clientHeight - 30) / 280,
    )
    const next = d3.zoomIdentity
      .translate(16, viewport.clientHeight / 2 - ((table.root.top + table.root.bottom) / 2) * scale)
      .scale(scale)

    if (selectionRef.current && zoomRef.current) {
      selectionRef.current.call(zoomRef.current.transform, next)
    } else {
      setTransform(next)
    }
  }

  const fitTable = () => {
    const viewport = viewportRef.current
    if (!viewport) return

    const scale = Math.min(
      (viewport.clientWidth - 30) / width,
      (viewport.clientHeight - 30) / table.height,
      1,
    )
    const next = d3.zoomIdentity
      .translate(15, (viewport.clientHeight - table.height * scale) / 2)
      .scale(scale)

    if (selectionRef.current && zoomRef.current) {
      selectionRef.current.call(zoomRef.current.transform, next)
    } else {
      setTransform(next)
    }
  }

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return

    const resize = () => {
      setSize({ width: viewport.clientWidth, height: viewport.clientHeight })
    }
    resize()

    const observer = new ResizeObserver(resize)
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (size.width > 1 && size.height > 1) focusRoot()
  }, [focusId, size.height, size.width])

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return

    const zoom = d3.zoom<HTMLDivElement, unknown>()
      .scaleExtent([0.002, 12])
      .wheelDelta((event) => -event.deltaY * 0.006)
      .on('zoom', (event) => setTransform(event.transform))
    const selection = d3.select(viewport)

    zoomRef.current = zoom
    selectionRef.current = selection
    selection.call(zoom).on('dblclick.zoom', null)

    return () => {
      selection.on('.zoom', null)
      zoomRef.current = null
      selectionRef.current = null
    }
  }, [])

  const lineage = useMemo(
    () => rootTrail.map((id) => ({ id, name: nodes[id]?.name ?? 'Unknown', dog: nodes[id] })),
    [nodes, rootTrail],
  )

  useEffect(() => {
    activeLineageRef.current?.scrollIntoView({ block: 'nearest' })
  }, [focusId, lineage.length])

  const globalExtremes = useMemo(() => {
    type Reachable = {
      id: string
      depth: number
      lineage: string[]
      hasParents: boolean
    }

    const reachable: Reachable[] = []
    const visit = (id: string | null, generation: number, path: Set<string>, ancestry: string[]) => {
      const dog = id ? nodes[id] : undefined
      if (!dog || !id || path.has(id) || reachable.length >= 100_000) return

      const nextPath = new Set(path).add(id)
      const parents = [dog.sire, dog.dam].filter(
        (parent): parent is string => Boolean(parent && nodes[parent]),
      )

      reachable.push({
        id,
        depth: generation,
        lineage: [...ancestry, id],
        hasParents: parents.length > 0,
      })
      parents.forEach((parent) => visit(parent, generation + 1, nextPath, [...ancestry, id]))
    }

    visit(rootId, 0, new Set(), [])

    const leaves = reachable.filter((node) => !node.hasParents)
    return {
      deepest: [...reachable].sort((left, right) => right.depth - left.depth).slice(0, 5),
      shallowDeadEnds: leaves.sort((left, right) => left.depth - right.depth).slice(0, 5),
      deepestGeneration: reachable.length ? Math.max(...reachable.map((node) => node.depth)) + 1 : 1,
    }
  }, [nodes, rootId])

  const pageTo = (id: string, ancestry: string[]) => onPage(id, ancestry)

  return (
    <section className="graph-panel pedigree-table-view">
      <div className="graph-header">
        <span>Standard pedigree table · Top: paternal · Bottom: maternal</span>
        <div className="table-actions">
          <label>
            Generations
            <input
              type="range"
              min="2"
              max="14"
              value={depth}
              onChange={(event) => onDepthChange(Number(event.target.value))}
            />
            <span>{depth}</span>
          </label>
          <button onClick={focusRoot}>Focus root</button>
          <button onClick={fitTable}>Fit table</button>
        </div>
      </div>

      <div className="table-metrics">
        <span><strong>{table.cells.length}</strong> cells shown</span>
        <span><strong>G{globalExtremes.deepestGeneration}</strong> deepest known path</span>
        <span><strong>G{rootTrail.length}</strong> current page focus</span>
      </div>

      <div className="pedigree-table-layout">
        <aside className="pedigree-context">
          <strong>Page lineage</strong>
          <small>Original root through the current table page focus.</small>
          <ol>
            {lineage.map(({ id, name, dog }, index) => (
              <li
                key={`${index}:${id}`}
                className={dog?.sex === 'Male' ? 'male' : dog?.sex === 'Female' ? 'female' : 'unknown'}
              >
                <button
                  ref={index === lineage.length - 1 ? activeLineageRef : undefined}
                  onClick={() => pageTo(id, lineage.slice(0, index + 1).map((item) => item.id))}
                >
                  {name}
                </button>
              </li>
            ))}
          </ol>
        </aside>

        <div className="pedigree-table-viewport" ref={viewportRef}>
          <div className="pedigree-generation-ruler">
            {Array.from({ length: depth + 2 }, (_, index) => (
              <i
                key={`line:${index}`}
                style={{ left: transform.x + index * cellWidth * transform.k }}
              />
            ))}
            {Array.from({ length: depth + 1 }, (_, generation) => (
              <span
                key={generation}
                style={{ left: transform.x + (generation * cellWidth + cellWidth / 2) * transform.k }}
              >
                {rootTrail.length + generation}
              </span>
            ))}
          </div>

          <div
            className="pedigree-table-layer"
            style={{
              width,
              height: table.height,
              transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.k})`,
            }}
          >
            {table.cells.map((cell) => {
              const viewTop = (24 - transform.y) / transform.k
              const viewBottom = (size.height - transform.y) / transform.k
              const center = (cell.top + cell.bottom) / 2
              const cellTop = cell.top + textBlockHeight / 2
              const cellBottom = cell.bottom - textBlockHeight / 2
              const minTextCenter = Math.max(cellTop, viewTop + textBlockHeight / 2)
              const maxTextCenter = Math.min(cellBottom, viewBottom - textBlockHeight / 2)
              const textY = minTextCenter > maxTextCenter
                ? center
                : Math.max(minTextCenter, Math.min(center, maxTextCenter))
              const birthYear = cell.dog.birth_date?.match(/(\d{4})$/)?.[1]
              const secondary = cell.dog.registration_number
                ?? (cell.dog.dobequest_id ? `DobeQuest ${cell.dog.dobequest_id}` : '')

              return (
                <button
                  key={cell.id}
                  className={cellClass(cell, highlightedIds)}
                  style={{
                    left: cell.depth * cellWidth,
                    top: cell.top,
                    height: cell.bottom - cell.top,
                  }}
                  onClick={() => pageTo(cell.canonicalId, [...rootTrail, ...cell.lineage.slice(1)])}
                  onDoubleClick={() => onSelect(cell.canonicalId)}
                >
                  <span className="pedigree-cell-content" style={{ top: textY - cell.top }}>
                    <strong>{cell.dog.name ?? 'Unknown dog'}</strong>
                    <small>{[secondary, birthYear].filter(Boolean).join(' · ')}</small>
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      </div>

      <div className="table-jump-panel">
        <strong>Jump within this page</strong>
        <div>
          <span>Deepest</span>
          {globalExtremes.deepest.map((node, index) => (
            <button
              key={`deep:${index}:${node.id}:${node.lineage.join('/')}`}
              onClick={() => pageTo(node.id, node.lineage)}
            >
              <small>G{node.depth + 1}</small>
              {nodes[node.id]?.name ?? 'Unknown dog'}
            </button>
          ))}
        </div>
        <div>
          <span>Shallow dead ends</span>
          {globalExtremes.shallowDeadEnds.map((node, index) => (
            <button
              key={`dead:${index}:${node.id}:${node.lineage.join('/')}`}
              onClick={() => pageTo(node.id, node.lineage)}
            >
              <small>G{node.depth + 1}</small>
              {nodes[node.id]?.name ?? 'Unknown dog'}
            </button>
          ))}
        </div>
      </div>
    </section>
  )
}
