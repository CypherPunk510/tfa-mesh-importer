# TFA `.mesh` format notes

Everything below was verified against the retail files. The header string in the container
reads `MeshSerializer_v1.100`.

These are the details that matter — get any of them wrong and the file “almost” parses,
which is much harder to debug than a hard failure.

---

## The two rules that bite

### 1. Chunk size includes the header

A chunk is:

```
uint16  chunk_id
uint32  chunk_size      # includes these 6 bytes
byte[]  payload         # chunk_size - 6
```

`chunk_size` is **not** the payload length. Subtract 6, or you will walk off the end.

### 2. Strings are `\n`-terminated, not length-prefixed

Material names, submesh names, texture aliases — all raw bytes terminated by `0x0A` (LF).
Read until `0x0A`, decode, done. There is no length field to skip.

---

## Chunk IDs

| ID | Meaning |
|---|---|
| `0x1000` | header |
| `0x3000` | mesh |
| `0x4000` | submesh |
| `0x4010` | submesh operation |
| `0x4100` | submesh bone assignment |
| `0x4200` | submesh texture alias |
| `0x5000` | geometry |
| `0x5100` | geometry vertex declaration |
| `0x5110` | geometry vertex element |
| `0x5200` | geometry vertex buffer |
| `0x5210` | geometry vertex buffer **data** (nested inside `0x5200`) |
| `0x6000` | mesh skeleton link |
| `0x7000` | mesh bone assignment |
| `0x8000` | mesh LOD |
| `0x9000` | mesh bounds |
| `0xA000` | submesh name table |
| `0xA100` | submesh name table element |

## Structure

```
M_MESH            (0x3000)
  bool  skeletallyAnimated
  M_SUBMESH       (0x4000)
    string  material
    bool    useSharedVertices
    uint32  indexCount
    byte    indexType flag
    ...     index data
    M_GEOMETRY    (0x5000)
      uint32  vertexCount
      M_GEOMETRY_VERTEX_DECLARATION   (0x5100)
        M_GEOMETRY_VERTEX_ELEMENT × n (0x5110)
      M_GEOMETRY_VERTEX_BUFFER        (0x5200)
        uint16 bindIndex
        uint16 vertexSize
        M_GEOMETRY_VERTEX_BUFFER_DATA (0x5210)
```

## Vertex element

Five `uint16` values, in this order:

| Field | Meaning |
|---|---|
| `source` | vertex buffer bind index |
| `offset` | byte offset inside the vertex |
| `type` | element type (see below) |
| `semantic` | what the data means (see below) |
| `index` | semantic index — `TEXCOORD 0/1`, `BLEND_WEIGHTS 0/1`, … |

### Element types

| ID | Struct | Components | Notes |
|---|---|---|---|
| 0–3 | `f` | 1–4 | float |
| 4 | `B` | 4 | packed `u32` colour, read as 4 bytes |
| 5–8 | `h` | 1–4 | half float |
| 9–11 | `B` | 4 | `UBYTE4` / colour variants |
| 12–15 | `d` | 1–4 | double |
| 16–19 | `H` | 1–4 | `ushort` |

### Semantics

| ID | Semantic |
|---|---|
| 1 | position |
| 2 | blend weights |
| 3 | blend indices |
| 4 | normal |
| 5 | diffuse |
| 6 | specular |
| 7 | texture coordinate |
| 8 | binormal |
| 9 | tangent |

## Submesh operations

| ID | Operation |
|---|---|
| 1 | point list |
| 2 | line list |
| 3 | line strip |
| 4 | triangle list |
| 5 | triangle strip |
| 6 | triangle fan |

Only triangle lists/strips/fans matter in practice for the shipped meshes; the importer
triangulates as needed.

---

## Practical tips

* Read the header chunk first and assert the version string — silent corruption later is
  almost always a wrong assumption here.
* Vertex buffers are **interleaved**. Build an unpack map from the declaration
  (`offset`, `type`, `semantic`) and slice each vertex once.
* `useSharedVertices` decides whether the indices refer to a shared buffer or a local one.
  Getting this backwards yields plausible-but-scrambled meshes.
* Keep an eye on `vertexSize` versus the declared element sizes: mismatches mean you
  mis-parsed the declaration.
* The game's mesh names and texture paths come from the material files, not from the
  `.mesh` — resolve them separately.
