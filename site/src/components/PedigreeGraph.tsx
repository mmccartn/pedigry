import * as d3 from 'd3'
import { useEffect, useMemo, useRef, useState } from 'react'
import { displayName, type DogNode } from '../types'

type BranchState = 'expanded' | 'shared_reference' | 'depth_limited' | 'missing'

type RenderNode = {
  id: string
  canonicalId: string | null
  depth: number
  state: BranchState
  label: string
  dog: DogNode | null
  x: number
  y: number
  children: RenderNode[]
}

type RenderEdge = {
  parent: RenderNode
  child: RenderNode
  relationship: 'sire' | 'dam'
}

type Props = {
  focusId: string
  nodes: Record<string, DogNode>
  depth: number
  onSelect: (id: string) => void
}

const nodeWidth = 132
const nodeHeight = 48
const horizontalGap = 54
const verticalGap = 124

function buildProjection(focusId: string, nodes: Record<string, DogNode>, maxDepth: number) {
  const seen = new Set<string>()
  const edges: RenderEdge[] = []
  let referenceCount = 0

  const visit = (id: string | null, depth: number, path: Set<string>): RenderNode => {
    const dog = id ? nodes[id] : undefined
    if (!id || !dog) {
      return { id: `missing:${depth}:${Math.random()}`, canonicalId: null, depth, state: 'missing', label: 'Unknown parent', dog: null, x: 0, y: 0, children: [] }
    }
    if (path.has(id) || seen.has(id)) {
      referenceCount += 1
      return { id: `shared:${id}:${referenceCount}`, canonicalId: id, depth, state: 'shared_reference', label: displayName(dog), dog, x: 0, y: 0, children: [] }
    }
    const node: RenderNode = { id, canonicalId: id, depth, state: 'expanded', label: displayName(dog), dog, x: 0, y: 0, children: [] }
    seen.add(id)
    if (depth === maxDepth) {
      if (dog.sire || dog.dam) node.state = 'depth_limited'
      return node
    }
    const nextPath = new Set(path).add(id)
    for (const relationship of ['sire', 'dam'] as const) {
      const child = visit(dog[relationship], depth + 1, nextPath)
      node.children.push(child)
      edges.push({ parent: child, child: node, relationship })
    }
    return node
  }

  const root = visit(focusId, 0, new Set())
  const leaves: RenderNode[] = []
  const assignX = (node: RenderNode): number => {
    if (node.children.length === 0) {
      node.x = leaves.length * (nodeWidth + horizontalGap) + 100
      leaves.push(node)
      return node.x
    }
    const childXs = node.children.map(assignX)
    node.x = (Math.min(...childXs) + Math.max(...childXs)) / 2
    return node.x
  }
  assignX(root)
  const maxObservedDepth = Math.max(...leaves.map((node) => node.depth), 0)
  const nodesList: RenderNode[] = []
  const collect = (node: RenderNode) => {
    node.y = 90 + (maxObservedDepth - node.depth) * verticalGap
    nodesList.push(node)
    node.children.forEach(collect)
  }
  collect(root)

  return {
    root,
    nodes: nodesList,
    edges,
    width: Math.max(900, leaves.length * (nodeWidth + horizontalGap) + 200),
    height: Math.max(480, (maxObservedDepth + 1) * verticalGap + 140),
  }
}

function drawRoundedRect(context: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, radius: number) {
  context.beginPath()
  context.roundRect(x, y, width, height, radius)
  context.fill()
  context.stroke()
}

export default function PedigreeGraph({ focusId, nodes, depth, onSelect }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const transformRef = useRef(d3.zoomIdentity)
  const [transform, setTransform] = useState(d3.zoomIdentity)
  const [hovered, setHovered] = useState<RenderNode | null>(null)
  const graph = useMemo(() => buildProjection(focusId, nodes, depth), [focusId, nodes, depth])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const zoom = d3.zoom<HTMLCanvasElement, unknown>().scaleExtent([0.18, 5]).on('zoom', (event) => {
      transformRef.current = event.transform
      setTransform(event.transform)
    })
    const selection = d3.select(canvas)
    selection.call(zoom).on('dblclick.zoom', null)
    const fit = Math.min(0.9, canvas.clientWidth / graph.width)
    selection.call(zoom.transform, d3.zoomIdentity.translate(24, 30).scale(fit))
    return () => {
      selection.on('.zoom', null)
    }
  }, [focusId, depth, graph.width])

  useEffect(() => {
    const canvas = canvasRef.current
    const container = containerRef.current
    if (!canvas || !container) return
    const ratio = window.devicePixelRatio || 1
    const width = container.clientWidth
    const height = 680
    canvas.width = width * ratio
    canvas.height = height * ratio
    canvas.style.width = `${width}px`
    canvas.style.height = `${height}px`
    const context = canvas.getContext('2d')
    if (!context) return
    context.setTransform(ratio, 0, 0, ratio, 0, 0)
    context.clearRect(0, 0, width, height)
    context.save()
    context.translate(transform.x, transform.y)
    context.scale(transform.k, transform.k)

    for (const edge of graph.edges) {
      context.beginPath()
      context.moveTo(edge.parent.x, edge.parent.y + nodeHeight / 2)
      context.bezierCurveTo(edge.parent.x, edge.parent.y + verticalGap * 0.58, edge.child.x, edge.child.y - verticalGap * 0.58, edge.child.x, edge.child.y - nodeHeight / 2)
      context.strokeStyle = edge.relationship === 'sire' ? '#4b7288' : '#9a7181'
      context.globalAlpha = 0.58
      context.lineWidth = 1.3
      context.stroke()
    }

    for (const node of graph.nodes) {
      const isShared = node.state === 'shared_reference'
      const isMissing = node.state === 'missing'
      context.globalAlpha = 1
      context.fillStyle = isMissing ? '#f3f5f5' : isShared ? '#fff8e9' : node.dog?.primary_source === 'dobequest' ? '#edf5e9' : '#ffffff'
      context.strokeStyle = isMissing ? '#aebdc4' : isShared ? '#d39b3d' : node.dog?.primary_source === 'dobequest' ? '#66874f' : '#2e6079'
      context.lineWidth = isShared ? 2.5 : 1.8
      if (isShared) context.setLineDash([5, 4])
      drawRoundedRect(context, node.x - nodeWidth / 2, node.y - nodeHeight / 2, nodeWidth, nodeHeight, 9)
      context.setLineDash([])

      if (transform.k > 0.4) {
        context.fillStyle = '#173a52'
        context.font = '600 12px Inter, sans-serif'
        context.textAlign = 'center'
        const label = isMissing ? node.label : node.label.length > 23 ? `${node.label.slice(0, 22)}…` : node.label
        context.fillText(label, node.x, node.y - 3)
        context.fillStyle = '#637b88'
        context.font = '11px Inter, sans-serif'
        const sublabel = isShared ? 'Shared lineage ↗' : node.state === 'depth_limited' ? 'Known ancestry continues' : node.dog?.registration_number ?? node.dog?.primary_source ?? ''
        context.fillText(sublabel, node.x, node.y + 15)
      }
    }
    context.restore()
  }, [graph, transform])

  const findNode = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current
    if (!canvas) return null
    const bounds = canvas.getBoundingClientRect()
    const x = (event.clientX - bounds.left - transform.x) / transform.k
    const y = (event.clientY - bounds.top - transform.y) / transform.k
    return graph.nodes.find((node) => Math.abs(x - node.x) <= nodeWidth / 2 && Math.abs(y - node.y) <= nodeHeight / 2) ?? null
  }

  return (
    <section className="graph-panel canvas-pedigree">
      <div className="graph-header">
        <div>
          <p className="eyebrow">Interactive ancestry</p>
          <h2>Pedigree graph</h2>
        </div>
        <p>Scroll to zoom, drag to pan, and select a dog to recenter the pedigree.</p>
      </div>
      <div className="graph-legend">
        <span><i className="legend-swatch akc" /> AKC-primary</span>
        <span><i className="legend-swatch dobequest" /> DobeQuest-primary</span>
        <span><i className="legend-swatch shared" /> Shared lineage shown elsewhere</span>
        <span><i className="legend-swatch missing" /> Missing parent data</span>
        <span><i className="legend-swatch depth" /> Known ancestry beyond depth</span>
      </div>
      <div className="graph-canvas" ref={containerRef}>
        <canvas
          ref={canvasRef}
          onClick={(event) => {
            const node = findNode(event)
            if (node?.canonicalId) onSelect(node.canonicalId)
          }}
          onMouseMove={(event) => setHovered(findNode(event))}
          onMouseLeave={() => setHovered(null)}
        />
        {hovered && (
          <div className="graph-tooltip">
            <strong>{hovered.label}</strong>
            <span>{hovered.state === 'shared_reference' ? 'Shared lineage; click to jump to primary occurrence' : hovered.state === 'depth_limited' ? 'Known ancestry exists beyond current depth' : hovered.state === 'missing' ? 'No known parent data' : hovered.dog?.registration_number ?? hovered.dog?.primary_source}</span>
          </div>
        )}
      </div>
    </section>
  )
}
