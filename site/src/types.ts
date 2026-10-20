export type DogNode = {
  name: string | null
  titles: string[]
  registration_number: string | null
  dobequest_id: string | null
  birth_date: string | null
  registration_type: string | null
  sex: string | null
  sire: string | null
  dam: string | null
  primary_source: 'akc' | 'dobequest'
}

export type Dataset = {
  generated_at: string
  nodes: Record<string, DogNode>
  stats: {
    akc_seed_records: number
    total_nodes: number
    dobequest_only_nodes: number
  }
}

export const displayName = (node: DogNode) => [node.titles.join(' '), node.name].filter(Boolean).join(' ')
