# -*- coding: utf-8 -*-
"""
Task Force Admiral (.mesh) importer for Blender
===============================================

Reads the OGRE binary meshes shipped with *Task Force Admiral*
(`media/models/**/*.mesh`) directly, with no external converter.

USAGE NOTICE
------------
Written to study how models are made: research into a commercial engine's asset
format, and into mesh construction. Personal study, hobby work, research and
education are welcome. COMMERCIAL USE IS NOT PERMITTED - no selling, no bundling
into paid products or services, no paid client work. No game assets are included
here; the game's content remains the property of its rights holders.
See LICENSE for the full terms.

Format notes (verified against the retail files, MeshSerializer_v1.100):
  * chunk header   : uint16 id + uint32 size, where **size includes the 6 byte header**
  * strings        : raw bytes terminated by 0x0A (LF), not length prefixed
  * M_MESH         : bool skeletallyAnimated, then sub-chunks
  * M_SUBMESH      : material string, bool useSharedVertices,
                     uint32 indexCount, 1 flag byte, index data, then sub-chunks
  * M_GEOMETRY     : uint32 vertexCount, then vertex declaration + vertex buffers
  * vertex element : 5 x uint16 -> source(bind), offset, type, semantic, index
  * vertex buffer  : uint16 bindIndex, uint16 vertexSize, nested 0x5210 data chunk

Usage as a Blender add-on
-------------------------
Copy this file to your Blender add-on folder
(e.g. .../Blender/5.1/scripts/addons/) and enable
"Task Force Admiral (.mesh)" in Preferences > Add-ons, then use
File > Import > Task Force Admiral Mesh (.mesh).

Usage as a plain script (batch conversions)
-------------------------------------------
    blender --background --python import_tfa_mesh.py -- <file-or-folder> [out.blend]
"""

bl_info = {
    "name": "Task Force Admiral OGRE Mesh (.mesh)",
    "author": "TFA Mesh Importer contributors",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > Task Force Admiral Mesh (.mesh)",
    "description": "Import Task Force Admiral OGRE binary .mesh files with materials and textures",
    "category": "Import-Export",
}

import os
import re
import struct
import math

# ---------------------------------------------------------------------------
# chunk ids
# ---------------------------------------------------------------------------
M_HEADER = 0x1000
M_MESH = 0x3000
M_SUBMESH = 0x4000
M_SUBMESH_OPERATION = 0x4010
M_SUBMESH_BONE_ASSIGNMENT = 0x4100
M_SUBMESH_TEXTURE_ALIAS = 0x4200
M_GEOMETRY = 0x5000
M_GEOMETRY_VERTEX_DECLARATION = 0x5100
M_GEOMETRY_VERTEX_ELEMENT = 0x5110
M_GEOMETRY_VERTEX_BUFFER = 0x5200
M_GEOMETRY_VERTEX_BUFFER_DATA = 0x5210
M_MESH_SKELETON_LINK = 0x6000
M_MESH_BONE_ASSIGNMENT = 0x7000
M_MESH_LOD = 0x8000
M_MESH_BOUNDS = 0x9000
M_SUBMESH_NAME_TABLE = 0xA000
M_SUBMESH_NAME_TABLE_ELEMENT = 0xA100

# vertex element type -> (struct char, component count, byte size)
VET = {
    0: ("f", 1), 1: ("f", 2), 2: ("f", 3), 3: ("f", 4),
    4: ("B", 4),                                   # VET_COLOUR (packed u32, read as 4 bytes)
    5: ("h", 1), 6: ("h", 2), 7: ("h", 3), 8: ("h", 4),
    9: ("B", 4), 10: ("B", 4), 11: ("B", 4),       # UBYTE4 / colour variants
    12: ("d", 1), 13: ("d", 2), 14: ("d", 3), 15: ("d", 4),
    16: ("H", 1), 17: ("H", 2), 18: ("H", 3), 19: ("H", 4),
}
VET_SIZE = {k: struct.calcsize("<" + v[0]) * v[1] for k, v in VET.items()}

# semantic ids
VES_POSITION = 1
VES_BLEND_WEIGHTS = 2
VES_BLEND_INDICES = 3
VES_NORMAL = 4
VES_DIFFUSE = 5
VES_SPECULAR = 6
VES_TEXCOORD = 7
VES_BINORMAL = 8
VES_TANGENT = 9

# submesh operations
OT_POINT_LIST = 1
OT_LINE_LIST = 2
OT_LINE_STRIP = 3
OT_TRIANGLE_LIST = 4
OT_TRIANGLE_STRIP = 5
OT_TRIANGLE_FAN = 6


# ---------------------------------------------------------------------------
# low level reader
# ---------------------------------------------------------------------------
class OgreReader(object):
    __slots__ = ("d", "p", "n")

    def __init__(self, data):
        self.d = data
        self.p = 0
        self.n = len(data)

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def raw(self, k):
        v = self.d[self.p:self.p + k]
        self.p += k
        return v

    def string(self):
        """Strings in this build are raw bytes terminated by LF."""
        i = self.d.find(b"\n", self.p)
        if i < 0:
            i = self.n
        s = self.d[self.p:i].decode("utf-8", "replace")
        self.p = i + 1
        return s

    def chunk(self):
        """Return (chunk_id, payload_start, chunk_end).

        `size` includes the 6 byte chunk header, so chunk_end = start + size.
        """
        start = self.p
        cid = self.u16()
        size = self.u32()
        if size < 6 or start + size > self.n:
            raise ValueError("corrupt chunk 0x%04X (size %d at offset %d)"
                             % (cid, size, start))
        return cid, self.p, start + size


def _find_chunk_end(d, start, end, wanted):
    """Locate a chunk of type `wanted` whose end matches `end` exactly."""
    needle = struct.pack("<H", wanted)
    p = d.find(needle, start, max(start, end - 6))
    while p != -1:
        size = struct.unpack_from("<I", d, p + 2)[0]
        if p + size == end:
            return p
        p = d.find(needle, p + 1, max(start, end - 6))
    return None


# ---------------------------------------------------------------------------
# mesh parsing
# ---------------------------------------------------------------------------
def parse_geometry(r, ge):
    """Parse an M_GEOMETRY chunk (payload already positioned)."""
    vertex_count = r.u32()
    declaration = []
    buffers = []
    while r.p < ge:
        cid, ps, ce = r.chunk()
        if cid == M_GEOMETRY_VERTEX_DECLARATION:
            while r.p < ce:
                eid, eps, ece = r.chunk()
                if eid == M_GEOMETRY_VERTEX_ELEMENT:
                    payload = ece - eps
                    if payload >= 10:
                        # verified layout: source, type, semantic, offset, index
                        source = r.u16()
                        vtype = r.u16()
                        semantic = r.u16()
                        offset = r.u16()
                        index = r.u16()
                    else:
                        source = offset = vtype = semantic = index = None
                    declaration.append({
                        "source": source, "offset": offset, "type": vtype,
                        "semantic": semantic, "index": index,
                    })
                r.p = ece
        elif cid == M_GEOMETRY_VERTEX_BUFFER:
            bind = r.u16()
            vertex_size = r.u16()
            vdata = b""
            while r.p < ce:
                did, dps, dce = r.chunk()
                if did == M_GEOMETRY_VERTEX_BUFFER_DATA:
                    vdata = r.raw(dce - dps)
                r.p = dce
            buffers.append({
                "bind": bind, "vertex_size": vertex_size, "data": vdata,
                "count": (len(vdata) // vertex_size) if vertex_size else 0,
            })
        r.p = ce
    return {
        "vertex_count": vertex_count,
        "declaration": declaration,
        "buffers": buffers,
    }


def parse_submesh(r, se):
    sm = {
        "material": r.string(),
        "name": None,
        "use_shared": False,
        "indices": [],
        "operation": OT_TRIANGLE_LIST,
        "geometry": None,
        "bones": [],
        "texture_aliases": {},
    }
    sm["use_shared"] = r.u8() != 0

    index_start = None
    index_count = 0
    if not sm["use_shared"]:
        index_count = r.u32()
        after_count = r.p
        # locate the following M_GEOMETRY chunk: the index data is preceded by
        # exactly one flag byte and uses either 16 or 32 bit indices, so try
        # the four possible layouts and keep the one that lands on a chunk.
        geo_pos = None
        width = 2
        starts = (after_count, after_count + 1) if index_count else (after_count,)
        for cand in starts:
            for w in ((2, 4) if index_count else (2,)):
                p = cand + index_count * w
                if p + 6 > se or r.d[p:p + 2] != struct.pack("<H", M_GEOMETRY):
                    continue
                size = struct.unpack_from("<I", r.d, p + 2)[0]
                if 6 <= size <= se - p:
                    geo_pos, width, index_start = p, w, cand
                    break
            if geo_pos is not None:
                break
        if index_start is None:
            index_start = after_count
        if index_count and index_start + index_count * width <= r.n:
            fmt = "<%d%s" % (index_count, "I" if width == 4 else "H")
            sm["indices"] = list(struct.unpack_from(fmt, r.d, index_start))
        r.p = geo_pos if geo_pos is not None else index_start + index_count * width

    while r.p < se:
        cid, ps, ce = r.chunk()
        if cid == M_GEOMETRY:
            sm["geometry"] = parse_geometry(r, ce)
        elif cid == M_SUBMESH_OPERATION:
            sm["operation"] = r.u16()
            try:
                r.u8()                                    # use32bitIndexes
            except Exception:
                pass
        elif cid == M_SUBMESH_BONE_ASSIGNMENT:
            try:
                vertex_index = r.u32()
                bone_index = r.u16()
                weight = struct.unpack_from("<f", r.d, r.p)[0]
                sm["bones"].append((vertex_index, bone_index, weight))
            except Exception:
                pass
        elif cid == M_SUBMESH_TEXTURE_ALIAS:
            alias = r.string()
            sm["texture_aliases"][alias] = r.string()
        r.p = ce
    return sm


def parse_mesh(path):
    """Parse a TFA/OGRE binary .mesh file, return a plain dict."""
    with open(path, "rb") as fh:
        data = fh.read()
    r = OgreReader(data)
    cid = r.u16()
    if cid != M_HEADER:
        raise ValueError("not an OGRE mesh file (magic 0x%04X)" % cid)
    version = r.string()
    cid, ps, me = r.chunk()
    if cid != M_MESH:
        raise ValueError("M_MESH chunk not found (got 0x%04X)" % cid)

    mesh = {
        "path": path,
        "version": version,
        "skeletally_animated": r.u8() != 0,
        "skeleton": None,
        "bounds": None,
        "shared_geometry": None,
        "submeshes": [],
        "names": {},
    }
    while r.p < me:
        cid, ps, ce = r.chunk()
        if cid == M_SUBMESH:
            mesh["submeshes"].append(parse_submesh(r, ce))
        elif cid == M_GEOMETRY:
            mesh["shared_geometry"] = parse_geometry(r, ce)
        elif cid == M_MESH_SKELETON_LINK:
            mesh["skeleton"] = r.string()
        elif cid == M_MESH_BOUNDS:
            vals = struct.unpack_from("<7f", r.d, r.p)
            mesh["bounds"] = {"min": vals[0:3], "max": vals[3:6], "radius": vals[6]}
        elif cid == M_SUBMESH_NAME_TABLE:
            while r.p < ce:
                eid, eps, ece = r.chunk()
                if eid == M_SUBMESH_NAME_TABLE_ELEMENT:
                    idx = r.u16()
                    mesh["names"][idx] = r.string()
                r.p = ece
        r.p = ce

    for i, sm in enumerate(mesh["submeshes"]):
        sm["name"] = mesh["names"].get(i)
    return mesh


# ---------------------------------------------------------------------------
# geometry -> plain arrays
# ---------------------------------------------------------------------------
def _element_size(decl, entry, vertex_size):
    """Byte size of one vertex element."""
    if entry["offset"] is not None:
        same = [e["offset"] for e in decl
                if e["source"] == entry["source"] and e["offset"] is not None]
        after = sorted(o for o in same if o > entry["offset"])
        if after:
            return after[0] - entry["offset"]
        if vertex_size:
            return max(0, vertex_size - entry["offset"])
    return VET_SIZE.get(entry["type"], 0)


def extract_vertices(geometry):
    """Turn a parsed geometry chunk into python lists.

    Returns dict with position / normal / uv / colour lists plus bone bindings.
    """
    vcount = geometry["vertex_count"]
    buffers = {}
    for b in geometry["buffers"]:
        buffers[b["bind"]] = b
        if not vcount:
            vcount = b["count"]

    positions = [None] * vcount
    normals = [None] * vcount
    uvs = [None] * vcount
    colors = [None] * vcount
    weights = [None] * vcount

    for entry in geometry["declaration"]:
        buf = buffers.get(entry["source"])
        if buf is None or not buf["data"]:
            continue
        vtype = entry["type"]
        semantic = entry["semantic"]
        if vtype not in VET:
            continue
        fmt, ncomp = VET[vtype]
        vertex_size = buf["vertex_size"]
        offset = entry["offset"]
        if offset is None:
            continue
        if semantic == VES_BLEND_INDICES:
            continue
        esize = _element_size(geometry["declaration"], entry, vertex_size)
        if esize <= 0:
            esize = VET_SIZE.get(vtype, 0)
        stride = vertex_size
        n = min(vcount, buf["count"])
        for i in range(n):
            base = i * stride + offset
            if base + esize > len(buf["data"]):
                continue
            if semantic == VES_POSITION:
                if fmt == "f" and esize >= 12:
                    positions[i] = struct.unpack_from("<3f", buf["data"], base)
                elif fmt == "f" and esize >= 8:
                    x, y = struct.unpack_from("<2f", buf["data"], base)
                    positions[i] = (x, y, 0.0)
            elif semantic == VES_NORMAL:
                if fmt == "f" and esize >= 12:
                    normals[i] = struct.unpack_from("<3f", buf["data"], base)
            elif semantic == VES_TEXCOORD and entry["index"] == 0:
                if fmt == "f" and esize >= 8:
                    u, v = struct.unpack_from("<2f", buf["data"], base)
                    uvs[i] = (u, v)
                elif fmt == "h" and esize >= 4:
                    u, v = struct.unpack_from("<2h", buf["data"], base)
                    uvs[i] = (u / 32767.0, v / 32767.0)
                elif fmt == "H" and esize >= 4:
                    u, v = struct.unpack_from("<2H", buf["data"], base)
                    uvs[i] = (u / 65535.0, v / 65535.0)
            elif semantic in (VES_DIFFUSE, VES_SPECULAR):
                if esize >= 4:
                    b0, g0, r0, a0 = struct.unpack_from("<4B", buf["data"], base)
                    colors[i] = (r0 / 255.0, g0 / 255.0, b0 / 255.0, a0 / 255.0)
            elif semantic in (VES_BLEND_WEIGHTS,):
                cnt = max(1, esize // 4)
                weights[i] = struct.unpack_from("<%df" % cnt, buf["data"], base)

    for i in range(vcount):
        if positions[i] is None:
            positions[i] = (0.0, 0.0, 0.0)
    return {
        "vertex_count": vcount,
        "positions": positions,
        "normals": normals,
        "uvs": uvs,
        "colors": colors,
        "weights": weights,
    }


def indices_to_faces(indices, operation):
    """Convert an index list plus primitive type into triangle tuples."""
    faces = []
    if operation == OT_TRIANGLE_LIST:
        for i in range(0, len(indices) - 2, 3):
            faces.append((indices[i], indices[i + 1], indices[i + 2]))
    elif operation == OT_TRIANGLE_STRIP:
        for i in range(len(indices) - 2):
            a, b, c = indices[i], indices[i + 1], indices[i + 2]
            if i % 2:
                a, b = b, a
            if a != b and b != c and a != c:
                faces.append((a, b, c))
    elif operation == OT_TRIANGLE_FAN:
        for i in range(1, len(indices) - 1):
            faces.append((indices[0], indices[i], indices[i + 1]))
    return faces


# ---------------------------------------------------------------------------
# material (.pmat) + texture lookup
# ---------------------------------------------------------------------------
_ALIAS_ROLE = {
    "diffuse": "base", "albedo": "base", "base": "base", "diffuse_map": "base",
    "normal": "normal", "normalmap": "normal", "normal_map": "normal",
    "aometallicroughness": "orm", "orm": "orm", "rm": "orm",
    "metallicroughness": "orm", "specular": "orm",
}


def parse_pmat(path):
    """Return {role: texture filename} for an OGRE .pmat material."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    out = {}
    for m in re.finditer(r"texture_unit\s+(\S+)\s*\{(.*?)\n\s*\}", text, re.S):
        unit_name, body = m.group(1), m.group(2)
        alias = re.search(r"texture_alias\s+(\S+)", body)
        tex = re.search(r"^\s*texture\s+(\S+)", body, re.M)
        if not tex:
            continue
        key = (alias.group(1) if alias else unit_name).strip().lower()
        role = _ALIAS_ROLE.get(key)
        if role and role not in out:
            out[role] = tex.group(1)
    return out


def resolve_texture(name, search_dirs):
    """Map e.g. 'Yamato_1941_C.png' to the shipped '..._C.png.dds' file."""
    if not name:
        return None
    base = os.path.basename(name)
    stem, ext = os.path.splitext(base)
    candidates = [base, base + ".dds", stem + ".dds", stem + ".tga",
                  stem + ".tga.dds", stem + ".png"]
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for cand in candidates:
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return None


def guess_textures(mesh_path, material_name, search_dirs):
    """Fallback for meshes whose .pmat is missing: guess the shipped maps.

    Looks for <something>_C / _NRM / _ORM combinations matching the material
    name first, then the mesh file name.
    """
    stems = []
    if material_name:
        stems.append(re.sub(r"[^A-Za-z0-9_\-]", "_", material_name))
    stems.append(os.path.splitext(os.path.basename(mesh_path))[0])
    stems.append(re.sub(r"_(body|details|simple|occlusion|LOD\d?)$", "",
                        os.path.splitext(os.path.basename(mesh_path))[0], flags=re.I))
    patterns = []
    for stem in stems:
        for suffix, role in (("_C.png", "base"), ("_NRM.png", "normal"),
                             ("_ORM.png", "orm"), ("_D.png", "base"),
                             ("_N.png", "normal")):
            patterns.append((stem + suffix, role))
    found = {}
    lowered = {}
    for d in search_dirs:
        if not d or not os.path.isdir(d):
            continue
        for entry in os.listdir(d):
            lowered.setdefault(entry.lower(), os.path.join(d, entry))
    for needle, role in patterns:
        if role in found:
            continue
        for cand in (needle + ".dds", needle):
            hit = lowered.get(cand.lower())
            if hit:
                found[role] = hit
                break
    return found


def find_material_file(material_name, mesh_path, media_roots):
    """Locate <material>.pmat for a material used by a mesh."""
    if not material_name:
        return None
    fname = material_name + ".pmat"
    here = os.path.dirname(mesh_path)
    candidates = [
        os.path.join(here, "materials", fname),
        os.path.join(os.path.dirname(here), "materials", fname),
        os.path.join(here, fname),
    ]
    for root in media_roots:
        candidates.append(os.path.join(root, "materials", fname))
    for c in candidates:
        if os.path.isfile(c):
            return c
    # shared modules (IJN_Weapons, IJN_Modules, planes\misc, ...) are scattered
    # all over media\models, so fall back to a global index built on first miss
    hit = asset_indexes(media_roots)["pmat"].get(material_name.lower())
    if hit:
        return hit
    model_dir = os.path.dirname(here) if os.path.basename(here) in ("models", "collisions") else here
    for dirpath, _dirnames, filenames in os.walk(model_dir):
        if fname in filenames:
            return os.path.join(dirpath, fname)
        if dirpath.count(os.sep) - model_dir.count(os.sep) > 2:
            _dirnames[:] = []
    return None


def material_search_dirs(mesh_path, pmat_path, media_roots):
    """Texture folders next to the mesh *and* next to its .pmat, in that order."""
    dirs = []
    if pmat_path:
        dirs.extend(texture_search_dirs(pmat_path, media_roots))
    dirs.extend(texture_search_dirs(mesh_path, media_roots))
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def texture_search_dirs(mesh_path, media_roots):
    here = os.path.dirname(mesh_path)
    parent = os.path.dirname(here)
    dirs = [
        os.path.join(parent, "textures"),
        os.path.join(here, "textures"),
        parent,
        here,
    ]
    for root in media_roots:
        dirs.append(os.path.join(root, "textures"))
        dirs.append(root)
    return dirs


def find_media_roots(path):
    """Walk up from a mesh path and collect every folder literally called 'media'."""
    roots = []
    p = os.path.abspath(path)
    for _ in range(8):
        p = os.path.dirname(p)
        if not p or p == os.path.dirname(p):
            break
        cand = os.path.join(p, "media")
        if os.path.isdir(cand):
            roots.append(cand)
            break
    return roots


_ASSET_INDEXES = {}


def asset_indexes(media_roots):
    """Global lookup of every .pmat and .mesh under media\\models (built once).

    TFA shares materials and meshes between models: turrets live in
    ``ships\\IJN_Weapons``, pilots in ``planes\\IJN_Pilots``, fittings in
    ``ships\\IJN_Modules`` and so on, so a per-model folder search is not enough.
    """
    key = tuple(media_roots)
    cached = _ASSET_INDEXES.get(key)
    if cached is not None:
        return cached
    pmats, meshes = {}, {}
    for root in media_roots:
        base = os.path.join(root, "models")
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                low = fn.lower()
                if low.endswith(".pmat"):
                    pmats.setdefault(fn[:-5].lower(), os.path.join(dirpath, fn))
                elif low.endswith(".mesh"):
                    meshes.setdefault(fn[:-5].lower(), os.path.join(dirpath, fn))
    cached = {"pmat": pmats, "mesh": meshes}
    _ASSET_INDEXES[key] = cached
    return cached


def find_module_mesh(name, media_roots):
    """Find a shared module mesh such as a turret (J94TNG46) by name."""
    if not name:
        return None
    return asset_indexes(media_roots)["mesh"].get(os.path.basename(name).lower())


# ---------------------------------------------------------------------------
# .parts files: the game's own part list, hierarchy and pivots
# ---------------------------------------------------------------------------
def find_parts_file(mesh_dir, media_roots=None):
    """`.parts` sits next to the `models` folder (and next to `collisions`)."""
    d = os.path.abspath(mesh_dir)
    if os.path.basename(d) in ("models", "collisions"):
        d = os.path.dirname(d)
    if not os.path.isdir(d):
        return None
    cands = [f for f in sorted(os.listdir(d)) if f.lower().endswith(".parts")]
    if not cands:
        return None
    wanted = os.path.basename(d).lower()
    for f in cands:
        if os.path.splitext(f)[0].lower().startswith(wanted):
            return os.path.join(d, f)
    return os.path.join(d, cands[0])


def _vec(text):
    try:
        parts = [float(v) for v in text.replace(",", " ").split()]
    except ValueError:
        return (0.0, 0.0, 0.0)
    while len(parts) < 3:
        parts.append(0.0)
    return tuple(parts[:3])


def parse_parts(path):
    """Parse a .parts file into parts / props / rudders / turrets / details."""
    out = {"parts": {}, "props": [], "rudders": [], "turrets": [],
           "details": [], "other": {}, "propeller_position": None}
    if not path or not os.path.isfile(path):
        return out
    section = None
    last = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            m = re.match(r"^\[\s*(\w+)\s*:\s*\d+\s*\]$", line)
            if m:
                section = m.group(1).lower()
                if section == "turret":
                    last = {"mesh": None, "offset": (0.0, 0.0, 0.0),
                            "azimuth": 0.0, "elevation": 0.0}
                    out["turrets"].append(last)
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            kl = key.lower()
            if kl == "propeller":
                last = {"mesh": val, "position": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0)}
                out["props"].append(last)
            elif kl == "propeller_position":
                if last is not None:
                    last["position"] = _vec(val)
                else:
                    # planes use a bare PROPELLER_POSITION key instead of
                    # the ship style propeller/propeller_position pair
                    out["propeller_position"] = _vec(val)
            elif kl == "propeller_scale" and last is not None:
                last["scale"] = _vec(val)
            elif kl == "rudder":
                last = {"mesh": val, "position": (0.0, 0.0, 0.0), "rotation": (0.0, 0.0, 0.0)}
                out["rudders"].append(last)
            elif kl == "rudder_position" and last is not None:
                last["position"] = _vec(val)
            elif kl == "rudder_rotation" and last is not None:
                last["rotation"] = _vec(val)
            elif kl == "detail_mesh":
                out["details"].append(val)
            elif section == "turret" and last is not None:
                if kl == "mesh":
                    last["mesh"] = val
                elif kl == "offset":
                    last["offset"] = _vec(val)
                elif kl == "defaultazimuth":
                    last["azimuth"] = _vec(val)[0]
                elif kl == "defaultelevation":
                    last["elevation"] = _vec(val)[0]
            elif key.isupper():
                out["parts"][key] = val
            else:
                out["other"].setdefault(key, val)
    return out


# ---------------------------------------------------------------------------
# Blender side
# ---------------------------------------------------------------------------
def _convert_coords(p, z_up=True):
    if not z_up:
        return p
    x, y, z = p
    return (x, -z, y)


def _finish_mesh(me, verts, uvs, normals, colors, face_materials, import_normals):
    """Fill UVs / normals / colours / material slots of a freshly built mesh."""
    import bpy
    # material indices must be set before validate(), otherwise dropped faces
    # shift the polygon list and every assignment is lost
    if len(me.polygons) == len(face_materials):
        for poly, mi in zip(me.polygons, face_materials):
            poly.material_index = mi
    if len(me.vertices) == len(verts):
        if import_normals and any(n is not None for n in normals):
            try:
                me.normals_split_custom_set_from_vertices(
                    [n if n is not None else (0.0, 0.0, 1.0) for n in normals])
            except Exception:
                pass
        if any(c is not None for c in colors):
            try:
                attr = me.color_attributes.new(name="Colour", type="FLOAT_COLOR", domain="POINT")
                for i, c in enumerate(colors):
                    if i < len(attr.data):
                        attr.data[i].color = c if c is not None else (1, 1, 1, 1)
            except Exception:
                pass
    uv_layer = me.uv_layers.new(name="UVMap")
    for lp in me.loops:
        vi = lp.vertex_index
        if vi < len(uvs):
            uv_layer.data[lp.index].uv = uvs[vi]


def build_objects(mesh_data, name, z_up=True, import_normals=True, split_submeshes=True,
                  flip_v=True):
    """Create Blender objects from parsed mesh data.

    Returns a list of (object, material_name) pairs. With ``split_submeshes``
    every sub-mesh becomes its own object (named after the sub-mesh), which is
    what makes turrets, cranes, gear and propellers animatable.

    ``flip_v`` mirrors the V texture coordinate: TFA/OGRE uses the Direct3D
    convention (V grows downwards, origin top-left) while Blender's UV origin is
    bottom-left, so without this every texture lands upside down.
    """
    import bpy

    shared = mesh_data["shared_geometry"]
    pieces = []
    for i, sm in enumerate(mesh_data["submeshes"]):
        geometry = shared if sm.get("use_shared") else sm.get("geometry")
        if geometry is not None:
            pieces.append((i, sm, geometry))
    if not pieces:
        return []

    if split_submeshes:
        batches = [[p] for p in pieces]
    else:
        batches = [pieces]

    made = []
    for batch in batches:
        verts, faces, uvs, normals, colors, face_materials = [], [], [], [], [], []
        materials = []
        mat_lookup = {}

        for index, sub, geometry in batch:
            data = extract_vertices(geometry)
            off = len(verts)
            n = data["vertex_count"]
            for k in range(n):
                verts.append(_convert_coords(data["positions"][k], z_up))
            sub_faces = indices_to_faces(sub["indices"], sub.get("operation", OT_TRIANGLE_LIST))
            sub_faces = [tuple(off + vi for vi in f) for f in sub_faces]
            faces.extend(sub_faces)

            uv_ok = any(u is not None for u in data["uvs"])
            nrm_ok = import_normals and any(v is not None for v in data["normals"])
            col_ok = any(c is not None for c in data["colors"])
            for k in range(n):
                uv = data["uvs"][k] if uv_ok else None
                if uv is None:
                    uv = (0.0, 0.0)
                elif flip_v:
                    uv = (uv[0], 1.0 - uv[1])
                uvs.append(uv)
                nrm = data["normals"][k] if nrm_ok else None
                normals.append(_convert_coords(nrm, z_up) if nrm is not None else None)
                colors.append(data["colors"][k] if col_ok else None)

            mat_name = sub["material"] or "TFA_material"
            if mat_name not in mat_lookup:
                mat_lookup[mat_name] = len(materials)
                materials.append(mat_name)
            face_materials.extend([mat_lookup[mat_name]] * len(sub_faces))

        if split_submeshes:
            index, sub, _g = batch[0]
            part_name = (sub.get("name") or "").strip()
            obj_name = part_name if part_name else "%s_%d" % (name, index)
        else:
            obj_name = name

        me = bpy.data.meshes.new(obj_name)
        me.from_pydata(verts, [], faces)
        me.validate(verbose=False)
        _finish_mesh(me, verts, uvs, normals, colors, face_materials, import_normals)

        obj = bpy.data.objects.new(obj_name, me)
        obj["tfa_mesh"] = name
        obj["tfa_part"] = obj_name
        made.append((obj, materials))
    return made


# ---------------------------------------------------------------------------
# skins / liveries
# ---------------------------------------------------------------------------
_ROLE_SUFFIXES = ("_C", "_NRM", "_ORM", "_D", "_N", "_SPEC")


def _skin_variant(tex_name, skin):
    """'B5N2_Kate_Model3_C.png' + 'HRY' -> 'B5N2_Kate_Model3_HRY_C.png'."""
    stem, ext = os.path.splitext(tex_name)
    for suf in _ROLE_SUFFIXES:
        if stem.lower().endswith(suf.lower()):
            return stem[:-len(suf)] + "_" + skin + stem[-len(suf):] + ext
    return stem + "_" + skin + ext


def list_skins(mesh_path, media_roots=None):
    """Skin tokens shipped for a model, e.g. ['AK', 'HRY', 'KA', 'Zuikaku']."""
    folder = os.path.dirname(os.path.abspath(mesh_path))
    model = os.path.basename(os.path.dirname(folder)) \
        if os.path.basename(folder).lower() in ("models", "collisions") \
        else os.path.basename(folder)
    prefix = model.lower() + "_"
    tokens = set()
    for d in texture_search_dirs(mesh_path, media_roots or find_media_roots(mesh_path)):
        if not d or not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.lower().endswith((".dds", ".png", ".tga")):
                continue
            stem = fn.split(".")[0]
            low = stem.lower()
            if low.startswith(prefix) and low.endswith("_c"):
                token = stem[len(prefix):-2]
                if token:
                    tokens.add(token)
    return sorted(tokens, key=str.lower)


# ---------------------------------------------------------------------------
# spinning propeller blur disc (the engine draws this procedurally in game)
# ---------------------------------------------------------------------------
def find_model_cfg(mesh_dir):
    """The model's `.cfg` (holds propellerRadius / propellerColour)."""
    d = os.path.abspath(mesh_dir)
    if os.path.basename(d) in ("models", "collisions"):
        d = os.path.dirname(d)
    if not os.path.isdir(d):
        return None
    wanted = os.path.basename(d).lower() + ".cfg"
    cands = [f for f in sorted(os.listdir(d)) if f.lower().endswith(".cfg")]
    for f in cands:
        if f.lower() == wanted:
            return os.path.join(d, f)
    for f in cands:
        if re.match(r"^[a-z0-9_]+\.cfg$", f.lower()):
            return os.path.join(d, f)
    return None


def parse_cfg(path):
    """Very small `key = value` reader for the TFA .cfg files."""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            out[key.strip()] = val.strip()
    return out


def estimate_blade_count(prop_obj):
    """Count blades from the tip positions of the real propeller mesh.

    The shaft is the local Y axis (the game's forward axis), so the blades live
    in the local X/Z plane.
    """
    verts = prop_obj.data.vertices
    if not verts:
        return 3
    pts = [(v.co.x, v.co.z) for v in verts]
    radii = [math.hypot(p[0], p[1]) for p in pts]
    rmax = max(radii) if radii else 0.0
    if rmax <= 0:
        return 3
    bins = [0] * 36
    for (px, pz), rr in zip(pts, radii):
        if rr > 0.55 * rmax:
            a = math.atan2(pz, px)
            bins[int(((a + math.pi) / (2.0 * math.pi)) * 36) % 36] = 1
    count = sum(1 for i in range(36) if bins[i] and not bins[i - 1])
    return count if 2 <= count <= 6 else 3


def _persist_image(img, name):
    """Make a generated image survive save/reload.

    `bpy.data.images.new()` creates a GENERATED image, whose custom pixels are
    *not* written to the .blend - on reload it falls back to a blank (black,
    alpha 1) buffer, which turned the blur disc into a solid black circle. So
    write it out once, load it back and pack it into the file.
    """
    import bpy
    import tempfile

    folder = bpy.app.tempdir or tempfile.gettempdir()
    tmp = os.path.join(folder, name + ".png")
    try:
        img.filepath_raw = tmp
        img.file_format = "PNG"
        img.save()
        colorspace = img.colorspace_settings.name
        bpy.data.images.remove(img)          # free the name before re-using it
        packed = bpy.data.images.load(tmp, check_existing=False)
        packed.name = name
        try:
            packed.colorspace_settings.name = colorspace
        except Exception:                                    # noqa: BLE001
            pass
        packed.alpha_mode = "STRAIGHT"
        try:
            packed.pack()
        except Exception:                                    # noqa: BLE001
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        packed["tfa_note"] = "generated spinning-propeller sprite (packed)"
        return packed
    except Exception:                                        # noqa: BLE001
        try:
            img.pack()
        except Exception:                                    # noqa: BLE001
            pass
        return img


def make_prop_blur_image(blades=3, size=512):
    """Generate a 'spinning propeller' sprite: dark disc plus blade smears."""
    import bpy
    import numpy as np

    n = int(size)
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    c = (n - 1) / 2.0
    x = (xx - c) / c
    y = (yy - c) / c
    r = np.sqrt(x * x + y * y)
    theta = np.arctan2(y, x)

    disc = np.clip((0.97 - r) / 0.10, 0.0, 1.0)
    alpha = disc * 0.20
    for k in range(max(1, int(blades))):
        a0 = 2.0 * np.pi * k / max(1, int(blades))
        d = np.abs(np.arctan2(np.sin(theta - a0), np.cos(theta - a0)))
        grow = np.clip((r - 0.10) / 0.25, 0.0, 1.0) * disc
        alpha += 0.34 * np.exp(-(d / 0.035) ** 2) * grow
        alpha += 0.15 * np.exp(-(d / 0.090) ** 2) * grow
    alpha += np.clip((0.11 - r) / 0.11, 0.0, 1.0) * 0.85
    alpha = np.clip(alpha, 0.0, 1.0)

    grey = 0.045 + 0.10 * np.clip(r, 0.0, 1.0)
    rgba = np.empty((n, n, 4), dtype=np.float32)
    rgba[..., 0] = grey
    rgba[..., 1] = grey
    rgba[..., 2] = grey
    rgba[..., 3] = alpha

    name = "TFA_prop_blur_%dblade" % blades
    img = bpy.data.images.new(name, n, n, alpha=True)
    img.pixels.foreach_set(rgba.ravel())
    img.alpha_mode = "STRAIGHT"
    img.update()
    img["tfa_note"] = "generated spinning-propeller sprite"
    return _persist_image(img, name)


def make_prop_blur_disc(prop_obj, radius, blades, collection=None, image=None):
    """Create the flat disc that carries the blur sprite, on the prop's pivot."""
    import bpy

    if image is None:
        image = make_prop_blur_image(blades)

    seg = 48
    verts = [(0.0, 0.0, 0.0)]
    uvs = [(0.5, 0.5)]
    for i in range(seg):
        a = 2.0 * math.pi * i / seg
        verts.append((radius * math.cos(a), 0.0, radius * math.sin(a)))
        uvs.append((0.5 + 0.5 * math.cos(a), 0.5 + 0.5 * math.sin(a)))
    faces = [(0, 1 + i, 1 + (i + 1) % seg) for i in range(seg)]

    me = bpy.data.meshes.new("propeller_blur")
    me.from_pydata(verts, [], faces)
    me.validate(verbose=False)
    uv = me.uv_layers.new(name="UVMap")
    for loop in me.loops:
        uv.data[loop.index].uv = uvs[loop.vertex_index]

    mat = bpy.data.materials.get("TFA_prop_blur")
    if mat is None:
        mat = bpy.data.materials.new("TFA_prop_blur")
    mat.use_nodes = True
    nt = mat.node_tree
    for node in list(nt.nodes):
        nt.nodes.remove(node)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (400, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (100, 0)
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tex.location = (-250, 0)
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    _set_input(bsdf, "Roughness", 0.45)
    _set_input(bsdf, "Metallic", 0.0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    for attr, val in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED")):
        try:
            setattr(mat, attr, val)
        except Exception:
            pass
    mat.use_backface_culling = False
    mat["tfa_note"] = ("generated spinning-propeller blur "
                       "(swap in a real blur map if you have one)")
    me.materials.append(mat)

    obj = bpy.data.objects.new("propeller_blur", me)
    obj["tfa_control"] = "propeller_blur"
    obj["tfa_note"] = "spinning propeller blur disc - radius %.2f m, %d blades" % (radius, blades)
    obj.location = prop_obj.location.copy()
    obj.rotation_euler = prop_obj.rotation_euler.copy()
    obj.scale = prop_obj.scale.copy()
    obj.parent = prop_obj.parent
    if collection is not None:
        collection.objects.link(obj)
    return obj


# Materials the engine defines itself (no .pmat ships with the game).
_ENGINE_MATERIAL_KINDS = {
    "glass_transparent": "glass",
    "glass": "glass",
    "aawires": "wire",
    "aawire": "wire",
    "plankton/pbs": "flat",
    "basewhitenolighting": "flat",
    "debugredlighted": "flat",
    "debugbluelighted": "flat",
    "no_mesh": "flat",
    "shadowdebug": "flat",
}

MISSING_TEXTURE_REPORT = []
_REPORTED_MATERIALS = set()


def _engine_kind(name):
    low = (name or "").lower()
    if low in _ENGINE_MATERIAL_KINDS:
        return _ENGINE_MATERIAL_KINDS[low]
    if "glass" in low or "transparent" in low:
        return "glass"
    if "wire" in low or "antenna" in low:
        return "wire"
    return None


def _set_input(node, name, value):
    if name in node.inputs:
        try:
            node.inputs[name].default_value = value
            return True
        except (TypeError, ValueError):
            pass
    return False


def make_blender_material(mat_name, pmat_path, search_dirs, mesh_path=None,
                          skin=None, flip_normal_green=False):
    """Build (or reuse) a Principled material for an OGRE material name.

    Returns ``(material, {role: path})``. ``skin`` prefers e.g.
    ``<model>_HRY_C.png`` over the .pmat default when that file exists.
    """
    import bpy

    if not skin and not flip_normal_green:
        mat = bpy.data.materials.get(mat_name)
        if mat is not None and mat.get("tfa_built"):
            return mat, {}
        if mat is None:
            mat = bpy.data.materials.new(mat_name)
    else:
        # a livery variant (or a flipped normal map) needs its own datablock
        key = "%s [%s%s]" % (mat_name, skin or "default",
                             " G-" if flip_normal_green else "")
        mat = bpy.data.materials.get(key)
        if mat is not None and mat.get("tfa_built"):
            return mat, {}
        if mat is None:
            mat = bpy.data.materials.new(key)
    mat.use_nodes = True
    nodes = mat.node_tree
    for n in list(nodes.nodes):
        nodes.nodes.remove(n)
    out = nodes.nodes.new("ShaderNodeOutputMaterial")
    out.location = (600, 0)
    bsdf = nodes.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (250, 0)
    nodes.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    roles = parse_pmat(pmat_path) if pmat_path else {}
    tex_paths = {}
    for role, fname in roles.items():
        p = resolve_texture(_skin_variant(fname, skin), search_dirs) if skin else None
        if not p:
            p = resolve_texture(fname, search_dirs)
        if p:
            tex_paths[role] = p
    if not tex_paths and mesh_path:
        tex_paths = guess_textures(mesh_path, mat_name, search_dirs)
        if skin:
            tex_paths = {r: (resolve_texture(_skin_variant(os.path.basename(v), skin),
                                             search_dirs) or v)
                         for r, v in tex_paths.items()}

    if "base" in tex_paths:
        img = bpy.data.images.load(tex_paths["base"], check_existing=True)
        tex = nodes.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.location = (-400, 250)
        nodes.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        if "Alpha" in tex.outputs and "Alpha" in bsdf.inputs:
            pass
    if "normal" in tex_paths:
        img = bpy.data.images.load(tex_paths["normal"], check_existing=True)
        img.colorspace_settings.name = "Non-Color"
        tex = nodes.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.location = (-400, -50)
        nmap = nodes.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-100, -50)
        nmap.inputs["Strength"].default_value = 1.0
        src = tex.outputs["Color"]
        if flip_normal_green:
            # Direct3D style normal maps keep +Y downwards; Blender expects +Y up
            sep = nodes.nodes.new("ShaderNodeSeparateColor")
            sep.location = (-300, -50)
            inv = nodes.nodes.new("ShaderNodeMath")
            inv.operation = "SUBTRACT"
            inv.location = (-200, -260)
            inv.inputs[0].default_value = 1.0
            comb = nodes.nodes.new("ShaderNodeCombineColor")
            comb.location = (-190, -50)
            nodes.links.new(tex.outputs["Color"], sep.inputs["Color"])
            nodes.links.new(sep.outputs["Green"], inv.inputs[1])
            nodes.links.new(sep.outputs["Red"], comb.inputs["Red"])
            nodes.links.new(inv.outputs[0], comb.inputs["Green"])
            nodes.links.new(sep.outputs["Blue"], comb.inputs["Blue"])
            src = comb.outputs["Color"]
        nodes.links.new(src, nmap.inputs["Color"])
        nodes.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    if "orm" in tex_paths:
        img = bpy.data.images.load(tex_paths["orm"], check_existing=True)
        img.colorspace_settings.name = "Non-Color"
        tex = nodes.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.location = (-400, -350)
        sep = nodes.nodes.new("ShaderNodeSeparateColor")
        sep.location = (-100, -350)
        nodes.links.new(tex.outputs["Color"], sep.inputs["Color"])
        # glTF style ORM packing: R = occlusion, G = roughness, B = metallic
        nodes.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
        nodes.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])

    kind = _engine_kind(mat_name)
    if not tex_paths:
        if kind == "glass":
            _set_input(bsdf, "Base Color", (0.72, 0.80, 0.84, 1.0))
            _set_input(bsdf, "Roughness", 0.06)
            _set_input(bsdf, "IOR", 1.45)
            _set_input(bsdf, "Alpha", 0.22)
            for attr, val in (("blend_method", "BLEND"),
                              ("shadow_method", "NONE"),
                              ("surface_render_method", "BLENDED")):
                try:
                    setattr(mat, attr, val)
                except Exception:
                    pass
            mat.use_backface_culling = False
            mat["tfa_note"] = "engine glass (no texture shipped)"
        elif kind == "wire":
            _set_input(bsdf, "Base Color", (0.06, 0.06, 0.06, 1.0))
            _set_input(bsdf, "Roughness", 0.45)
            mat["tfa_note"] = "engine wire material (no texture shipped)"
        else:
            _set_input(bsdf, "Base Color", (0.55, 0.55, 0.55, 1.0))
            _set_input(bsdf, "Roughness", 0.6)
            mat["tfa_note"] = "no texture found"
            if mat_name not in _REPORTED_MATERIALS:
                _REPORTED_MATERIALS.add(mat_name)
                MISSING_TEXTURE_REPORT.append(
                    "%s -- %s" % (mat_name, "pmat found but textures missing"
                                  if pmat_path else "no .pmat in the game (engine material)"))
    elif kind == "glass":
        _set_input(bsdf, "Alpha", 0.35)
        try:
            mat.surface_render_method = "BLENDED"
        except Exception:
            pass
    mat["tfa_built"] = True
    return mat, tex_paths


def import_mesh_file(path, z_up=True, import_textures=True, import_normals=True,
                     collection=None, split_submeshes=True, skin=None, flip_v=True,
                     flip_normal_green=False):
    """Import one .mesh file, return the list of created objects."""
    import bpy

    data = parse_mesh(path)
    base = os.path.splitext(os.path.basename(path))[0]
    media_roots = find_media_roots(path)
    coll = collection if collection is not None else bpy.context.scene.collection

    objs = []
    for obj, material_names in build_objects(data, base, z_up=z_up,
                                             import_normals=import_normals,
                                             split_submeshes=split_submeshes,
                                             flip_v=flip_v):
        coll.objects.link(obj)
        for mat_name in material_names:
            if import_textures:
                pmat = find_material_file(mat_name, path, media_roots)
                dirs = material_search_dirs(path, pmat, media_roots)
                mat, _tex = make_blender_material(mat_name, pmat, dirs, mesh_path=path,
                                                  skin=skin,
                                                  flip_normal_green=flip_normal_green)
            else:
                mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
            obj.data.materials.append(mat)
        objs.append(obj)
    return objs


# ---------------------------------------------------------------------------
# part hierarchy / pivots from the game's .parts files
# ---------------------------------------------------------------------------
def _conv_vec(v, z_up=True):
    """game (Y-up) vector -> Blender (Z-up)."""
    if not z_up:
        return tuple(v)
    return (v[0], -v[2], v[1])


def set_object_origin(obj, pivot):
    """Move an object's origin onto `pivot` without moving the geometry."""
    from mathutils import Matrix, Vector
    p = Vector(pivot)
    if obj.data is not None:
        obj.data.transform(Matrix.Translation(-p))
    obj.location = Vector(obj.location) + p


def place_part(obj, pivot):
    """Put a part on `pivot`.

    Propellers and rudders are modelled around their own hub/stock (verified:
    the Kate's propeller bbox centre is within 0.4 m of its origin, the Yamato
    propeller's within 1.1 m), so they only need to be *moved*, not re-centred.
    """
    from mathutils import Vector
    obj.location = Vector(pivot)


def mesh_centre(obj):
    """Centre of an object's mesh bounding box (local space)."""
    verts = obj.data.vertices
    if not verts:
        return (0.0, 0.0, 0.0)
    lo = [1e30] * 3
    hi = [-1e30] * 3
    for v in verts:
        co = v.co
        for i in range(3):
            if co[i] < lo[i]:
                lo[i] = co[i]
            if co[i] > hi[i]:
                hi[i] = co[i]
    return tuple((lo[i] + hi[i]) / 2.0 for i in range(3))


def mesh_size(obj):
    """Mesh bounding-box extent (local space)."""
    verts = obj.data.vertices
    if not verts:
        return (0.0, 0.0, 0.0)
    lo = [1e30] * 3
    hi = [-1e30] * 3
    for v in verts:
        co = v.co
        for i in range(3):
            if co[i] < lo[i]:
                lo[i] = co[i]
            if co[i] > hi[i]:
                hi[i] = co[i]
    return tuple(hi[i] - lo[i] for i in range(3))


def place_module(obj, pivot):
    """Place a shared module (turret/gun) on `pivot`.

    Module meshes are *not* modelled around their own origin - they keep the
    coordinates of whichever ship first used them - so they have to be
    re-centred before being moved onto the barbette position.
    """
    from mathutils import Matrix, Vector
    centre = mesh_centre(obj)
    if obj.data is not None and any(centre):
        obj.data.transform(Matrix.Translation(-Vector(centre)))
    obj.location = Vector(pivot)


_LOD_SUFFIXES = ("_simple", "_lod0")


def filter_mesh_files(files, skip_lods=True):
    """Drop engine-only helper meshes and low-detail duplicates."""
    keep = []
    for f in files:
        low = os.path.splitext(os.path.basename(f))[0].lower()
        # the *_occlusion meshes are invisible culling proxies in game
        if low.endswith(("_occlusion", "_occluder")):
            continue
        keep.append(f)
    files = keep
    if not skip_lods:
        return list(files)
    stems = set()
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0]
        low = stem.lower()
        for suf in _LOD_SUFFIXES:
            if low.endswith(suf):
                stems.add(low[:-len(suf)])
    keep = []
    for f in files:
        low = os.path.splitext(os.path.basename(f))[0].lower()
        dropped = False
        for suf in _LOD_SUFFIXES:
            if low.endswith(suf) and low[:-len(suf)] in stems:
                dropped = True
                break
        if not dropped:
            keep.append(f)
    return keep


# the game keeps both states of these parts in the model; only one is used at a
# time. GEAR_OPEN is the "wheels down" set, hidden because models are normally
# flown gear-up (Alt+H brings it back).
_VARIANT_KEYS_TO_HIDE = ("GEAR_OPEN", "GEAR_DOWN", "PROPELLER_STATIC")
_HELPER_MESH_SUFFIXES = ("_wake", "_occlusion")


def group_parts(objects, mesh_dir, z_up=True, media_roots=None, assemble_modules=True,
                collection=None, model_name=None, hide_variants=True,
                flip_normal_green=False, prop_blur=True):
    """Parent imported parts under one root Empty and restore the game pivots.

    Uses the `.parts` file that ships next to the model's `models` folder, which
    also lists propeller/rudder positions and the turret offsets.
    """
    import bpy
    from math import radians

    if media_roots is None:
        media_roots = find_media_roots(mesh_dir)
    parts_path = find_parts_file(mesh_dir, media_roots)
    parts = parse_parts(parts_path)

    folder = os.path.abspath(mesh_dir)
    if os.path.basename(folder) in ("models", "collisions"):
        folder = os.path.dirname(folder)
    root_name = model_name or os.path.basename(folder) or "TFA_model"
    coll = collection if collection is not None else bpy.context.scene.collection

    root = bpy.data.objects.new(root_name, None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = 4.0
    coll.objects.link(root)
    root["tfa_root"] = True
    if parts_path:
        root["tfa_parts_file"] = os.path.basename(parts_path)

    by_mesh = {}
    for o in objects:
        by_mesh.setdefault(o.get("tfa_mesh", o.name), []).append(o)

    for o in objects:
        o.parent = root

    # plane style: PROPELLER_RUNNING / PROPELLER_STOPPED + one shared position
    plane_props = [(k, v) for k, v in parts["parts"].items()
                   if k.upper().startswith("PROPELLER") and k.upper() != "PROPELLER_POSITION"]
    prop_objs = {}
    if plane_props and parts.get("propeller_position"):
        pivot = _conv_vec(parts["propeller_position"], z_up)
        for key, mesh_name in plane_props:
            for o in by_mesh.get(mesh_name, []):
                place_part(o, pivot)
                o["tfa_control"] = "propeller"
                prop_objs[key.upper()] = o

    for spec in parts["props"]:
        for o in by_mesh.get(spec["mesh"], []):
            place_part(o, _conv_vec(spec["position"], z_up))
            scale = _conv_vec(spec["scale"], z_up)
            o.scale = tuple(abs(v) if v else 1.0 for v in scale)
            o["tfa_control"] = "propeller"
    for spec in parts["rudders"]:
        for o in by_mesh.get(spec["mesh"], []):
            place_part(o, _conv_vec(spec["position"], z_up))
            rx, ry, rz = spec["rotation"]
            if rx or ry or rz:
                o.rotation_euler = (radians(rx), radians(-rz), radians(ry))
            o["tfa_control"] = "rudder"

    made_modules = []
    missing, skipped = [], []
    # only assemble the shared weapon modules when the whole model is being
    # imported - pulling in 36 turrets while the user asked for one rudder
    # would be surprising
    present = set(o.get("tfa_mesh", "") for o in objects)
    full_model = True
    if parts["parts"].get("FUSELAGE"):
        full_model = parts["parts"]["FUSELAGE"] in present
    elif parts["details"]:
        full_model = any(d in present for d in parts["details"])
    for i, spec in enumerate(parts["turrets"]):
        name = spec.get("mesh")
        if not name:
            continue
        if not (assemble_modules and full_model):
            skipped.append(name)
            continue
        module = find_module_mesh(name, media_roots)
        if not module:
            missing.append(name)
            continue
        try:
            objs = import_mesh_file(module, z_up=z_up, import_textures=True,
                                    import_normals=True, collection=coll,
                                    split_submeshes=False,
                                    flip_normal_green=flip_normal_green)
        except Exception:                                    # noqa: BLE001
            missing.append(name)
            continue
        for o in objs:
            o.name = "%s_turret%d" % (name, i)
            place_module(o, _conv_vec(spec["offset"], z_up))
            if spec.get("azimuth"):
                o.rotation_euler = (0.0, 0.0, radians(spec["azimuth"]))
            o.parent = root
            o["tfa_control"] = "turret"
            made_modules.append(o)

    # helper meshes (wake footprint, culling proxies) are invisible in game
    hidden_helpers, hidden_variants = [], []
    for o in objects:
        low = str(o.get("tfa_mesh", "")).lower()
        if low.endswith(_HELPER_MESH_SUFFIXES):
            o.hide_viewport = True
            o.hide_render = True
            o["tfa_note"] = "engine helper mesh (hidden by default)"
            hidden_helpers.append(o.name)
    if hide_variants:
        for key, mesh_name in parts["parts"].items():
            if key.upper() in _VARIANT_KEYS_TO_HIDE:
                for o in by_mesh.get(mesh_name, []):
                    o.hide_viewport = True
                    o.hide_render = True
                    o["tfa_variant"] = "unused variant of %s (Alt+H to show)" % key
                    hidden_variants.append(o.name)
        # "PROPELLER_RUNNING" is only a small placeholder for the blurred disc the
        # engine draws procedurally (propellerRadius / propellerColour in the .cfg),
        # so keep the real bladed propeller visible and hide the placeholder
        run = prop_objs.get("PROPELLER_RUNNING")
        stop = prop_objs.get("PROPELLER_STOPPED")
        if run is not None and stop is not None:
            def _extent(o):
                sz = mesh_size(o)
                return max(sz) if sz else 0.0
            placeholder = run if _extent(run) < _extent(stop) else stop
            blades = stop if placeholder is run else run
            placeholder.hide_viewport = placeholder.hide_render = True
            placeholder["tfa_variant"] = ("engine placeholder for the blurred prop disc "
                                          "(Alt+H to show)")
            hidden_variants.append(placeholder.name)
            blades["tfa_note"] = "rotate around local Y to spin the propeller"

    # optional: build the flat spinning-prop blur disc the engine draws in shader
    blur_obj = None
    if prop_blur and plane_props and parts.get("propeller_position"):
        cfg = parse_cfg(find_model_cfg(mesh_dir))
        try:
            radius = float(cfg.get("propellerRadius") or 0.0)
        except ValueError:
            radius = 0.0
        # the two propeller meshes are "spinner only" (small) and
        # "spinner + blades" (large); with the blur disc we keep the spinner
        found = []
        for key in ("PROPELLER_RUNNING", "PROPELLER_STOPPED"):
            mesh_name = parts["parts"].get(key)
            if mesh_name and by_mesh.get(mesh_name):
                found.append(by_mesh[mesh_name][0])
        spinner = bladed = None
        if len(found) == 2:
            if max(mesh_size(found[0])) < max(mesh_size(found[1])):
                spinner, bladed = found[0], found[1]
            else:
                spinner, bladed = found[1], found[0]
        elif found:
            bladed = found[0]
        if radius > 0 and (bladed is not None or spinner is not None):
            ref = bladed or spinner
            n_blades = estimate_blade_count(ref)
            blur_obj = make_prop_blur_disc(ref, radius, n_blades, collection=coll)
            blur_obj.name = "propeller_blur_0"
            if spinner is not None:
                # the protruding spinner / axle stays visible through the disc
                spinner.hide_viewport = spinner.hide_render = False
                try:
                    del spinner["tfa_variant"]
                except (KeyError, TypeError):
                    pass
                spinner["tfa_note"] = "spinner kept visible while the blur disc is used"
                if spinner.name in hidden_variants:
                    hidden_variants.remove(spinner.name)
            if bladed is not None and bladed is not spinner:
                bladed.hide_viewport = bladed.hide_render = True
                bladed["tfa_variant"] = ("blades hidden while the blur disc is used "
                                         "(Alt+H to show them)")
                if bladed.name not in hidden_variants:
                    hidden_variants.append(bladed.name)
            print("[tfa] spinning-propeller blur disc: radius %.2f m, %d blades, spinner=%s"
                  % (radius, n_blades, spinner.name if spinner else "none"))

    info = {
        "root": root,
        "parts_file": parts_path,
        "full_model": full_model,
        "props": len(parts["props"]) + (len(plane_props) if parts.get("propeller_position") else 0),
        "rudders": len(parts["rudders"]),
        "turrets": len(made_modules),
        "propeller_blur": blur_obj.name if blur_obj else None,
        "missing_modules": missing,
        "skipped_modules": skipped,
        "hidden_variants": hidden_variants,
        "hidden_helpers": hidden_helpers,
        "other_parts": parts["other"],
    }
    return root, info


def import_model_folder(folder, z_up=True, import_textures=True, import_normals=True,
                        collection=None, split_submeshes=True, group=True,
                        assemble_modules=True, skip_lods=True, model_name=None,
                        hide_variants=True, skin=None, flip_v=True,
                        flip_normal_green=False, prop_blur=True,
                        marking_unit=None, marking_number=301,
                        signature_text=None, signature_height=0.17,
                        signature_colour=(0.92, 0.92, 0.90, 1.0), signature_font="usn",
                        signature_place="fin", signature_along=0.68):
    """Import every .mesh of one model folder; optionally build the part hierarchy."""
    files = []
    if os.path.isdir(folder):
        files = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                 if f.lower().endswith(".mesh")]
    files = filter_mesh_files(files, skip_lods)
    objects = []
    for p in files:
        objects.extend(import_mesh_file(p, z_up=z_up, import_textures=import_textures,
                                        import_normals=import_normals,
                                        collection=collection,
                                        split_submeshes=split_submeshes,
                                        skin=skin, flip_v=flip_v,
                                        flip_normal_green=flip_normal_green))
    info = {}
    root = None
    if group and objects:
        root, info = group_parts(objects, folder, z_up=z_up, assemble_modules=assemble_modules,
                                 collection=collection, model_name=model_name,
                                 hide_variants=hide_variants,
                                 flip_normal_green=flip_normal_green,
                                 prop_blur=prop_blur)
    if files:
        info["skins"] = list_skins(files[0])
        info["skin_used"] = skin or ""
        info["marking_units"] = list_marking_units(folder)
        if marking_unit:
            _marks, minfo = make_markings(folder, marking_unit, marking_number,
                                          collection=collection, parent=root,
                                          wrap_target=_marking_target(objects))
            info["markings"] = minfo
        if signature_text:
            _sig, sinfo = make_signature(folder, signature_text, height=signature_height,
                                         colour=tuple(signature_colour),
                                         collection=collection, parent=root,
                                         wrap_target=_marking_target(objects),
                                         font_choice=signature_font,
                                         place=signature_place, y_frac=signature_along)
            info["signature"] = sinfo
    return root, objects, info


# ---------------------------------------------------------------------------
# Blender add-on plumbing
# ---------------------------------------------------------------------------
try:
    import bpy
    from bpy.props import BoolProperty, StringProperty
    from bpy_extras.io_utils import ImportHelper

    class IMPORT_OT_tfa_mesh(bpy.types.Operator, ImportHelper):
        """Import Task Force Admiral (.mesh)"""
        bl_idname = "import_scene.tfa_mesh"
        bl_label = "Task Force Admiral Mesh (.mesh)"
        bl_options = {"UNDO", "PRESET"}

        # Blender 4.2+/5.x needs annotated properties; since ImportHelper no
        # longer declares `files` / `directory`, declare them here.
        filename_ext = ".mesh"
        filter_glob: StringProperty(default="*.mesh", options={"HIDDEN"})
        files: bpy.props.CollectionProperty(name="File Path",
                                            type=bpy.types.OperatorFileListElement)
        directory: StringProperty(subtype="DIR_PATH")

        z_up: BoolProperty(name="Convert to Z-up", default=True,
                           description="OGRE models are Y-up, rotate them into Blender's Z-up space")
        import_textures: BoolProperty(name="Import .pmat + textures", default=True,
                                      description="Resolve materials and load the shipped DDS textures")
        import_normals: BoolProperty(name="Import custom normals", default=True)
        split_submeshes: BoolProperty(name="Split sub-meshes into objects", default=True,
                                      description="One object per sub-mesh (turrets, cranes, glass, "
                                                  "gear ...) instead of a single merged object")
        group_parts: BoolProperty(name="Group parts under a root Empty", default=True,
                                  description="Parent every part to one Empty so the model moves "
                                              "as a whole")
        assemble_modules: BoolProperty(name="Assemble turret/gun modules", default=True,
                                       description="Pull the shared weapon meshes listed in the "
                                                   ".parts file and place them at their offsets")
        skip_lods: BoolProperty(name="Skip low-detail variants", default=True,
                                description="Ignore *_Simple meshes when the full-detail mesh "
                                            "is present in the same folder")
        hide_variants: BoolProperty(name="Hide unused part variants", default=True,
                                    description="Hide the on-ground/stopped duplicates "
                                                "(GEAR_OPEN, PROPELLER_STOPPED); Alt+H shows them")
        flip_v: BoolProperty(name="Flip V (D3D -> Blender)", default=True,
                             description="TFA/OGRE puts V=0 at the top of the texture, Blender at "
                                         "the bottom; turning this off makes every texture upside "
                                         "down (only useful for comparison)")
        flip_normal_green: BoolProperty(name="Flip normal map green", default=False,
                                        description="Enable if surface detail looks embossed "
                                                    "inverted (Direct3D style normal maps)")
        skin: StringProperty(name="Livery / skin", default="",
                             description="Optional skin token such as HRY, AK, KA, SRY, Zuikaku, "
                                         "TomonagaV1; empty = the .pmat default. The tokens that "
                                         "exist for the model are printed in the console")
        prop_blur: BoolProperty(name="Spinning propeller blur disc", default=True,
                                description="Generate the flat blur disc the engine draws in "
                                            "shader (radius from propellerRadius in the .cfg) and "
                                            "hide the bladed propeller; untick to get the real "
                                            "three-blade propeller instead")
        marking_unit: StringProperty(name="Unit markings", default="",
                                     description="Squadron/unit tail codes from the game's .mrk "
                                                 "files, e.g. Zuikaku, Shokaku, Akagi, Kaga, "
                                                 "Soryu, Hiryu, Shoho; empty = none. The units "
                                                 "available for the model are printed in the "
                                                 "console")
        marking_number: bpy.props.IntProperty(name="Tail number", default=301, min=1, max=999,
                                              description="Aircraft number, e.g. 301 renders as "
                                                          "EII-301 for Zuikaku")
        signature_text: StringProperty(name="Tail signature", default="",
                                       description="Custom text painted on both sides of the tail "
                                                   "(e.g. your channel name); placed where the "
                                                   "squadron tail code would go")
        signature_height: bpy.props.FloatProperty(name="Signature height", default=0.14, min=0.03,
                                                  max=1.0,
                                                  description="Cap height in metres per line")
        signature_colour: bpy.props.FloatVectorProperty(name="Signature colour", subtype="COLOR",
                                                        size=4, min=0.0, max=1.0,
                                                        default=(0.92, 0.92, 0.90, 1.0))
        signature_font: StringProperty(name="Signature font", default="usn",
                                       description="'usn' = the game's US stencil font (readable "
                                                   "Latin), 'ijn' = the Japanese tail-code font "
                                                   "(Latin letters look like katakana), or a path "
                                                   "to your own .ttf/.otf")
        signature_place: StringProperty(name="Signature place", default="fin",
                                        description="'fin' = the vertical tail (default, clear of "
                                                    "the fuselage roundel); 'fuselage' = the rear "
                                                    "fuselage flank")
        signature_along: bpy.props.FloatProperty(name="Signature position", default=0.68, min=0.05,
                                                 max=0.95,
                                                 description="How far aft to sit when placed on "
                                                             "the fuselage (0 = nose, 1 = tail)")

        def execute(self, context):
            paths = [os.path.join(self.directory, f.name) for f in self.files] \
                if self.files else [self.filepath]
            paths = [p for p in paths if p.lower().endswith(".mesh")]
            if not paths:
                self.report({"ERROR"}, "no .mesh file selected")
                return {"CANCELLED"}

            report_start = len(MISSING_TEXTURE_REPORT)
            by_dir = {}
            for p in paths:
                by_dir.setdefault(os.path.dirname(p), []).append(p)

            n_files = n_objects = 0
            roots, missing_mods = [], []
            for folder, files in sorted(by_dir.items()):
                files = filter_mesh_files(files, self.skip_lods)
                parent = os.path.dirname(folder)
                label = os.path.basename(parent) if os.path.basename(folder) in (
                    "models", "collisions") else os.path.basename(folder)
                coll = bpy.data.collections.new("TFA_" + (label or "import"))
                context.scene.collection.children.link(coll)

                objects = []
                for p in files:
                    try:
                        objects.extend(import_mesh_file(
                            p, z_up=self.z_up, import_textures=self.import_textures,
                            import_normals=self.import_normals, collection=coll,
                            split_submeshes=self.split_submeshes,
                            skin=self.skin or None, flip_v=self.flip_v,
                            flip_normal_green=self.flip_normal_green))
                    except Exception as exc:                     # noqa: BLE001
                        self.report({"ERROR"}, "%s: %s" % (os.path.basename(p), exc))
                n_files += len(files)
                n_objects += len(objects)
                if objects:
                    skins = list_skins(files[0])
                    print("[tfa] %s: %d file(s), available liveries: %s"
                          % (label, len(files), ", ".join(skins) if skins else "(none)"))
                    if self.skin and skins and self.skin not in skins:
                        print("[tfa] WARNING: livery %r not found, using the default textures"
                              % self.skin)
                if self.group_parts and objects:
                    root, info = group_parts(objects, folder, z_up=self.z_up,
                                             assemble_modules=self.assemble_modules,
                                             collection=coll,
                                             hide_variants=self.hide_variants,
                                             flip_normal_green=self.flip_normal_green,
                                             prop_blur=self.prop_blur)
                    roots.append(root.name)
                    missing_mods.extend(info.get("missing_modules", []))
                    n_objects += info.get("turrets", 0)      # assembled modules
                if self.marking_unit and objects:
                    units = list_marking_units(folder)
                    print("[tfa] %s: units with markings: %s"
                          % (label, ", ".join(units) if units else "(none)"))
                    marks, minfo = make_markings(folder, self.marking_unit,
                                                 self.marking_number,
                                                 collection=coll,
                                                 parent=objects[0].parent or objects[0],
                                                 wrap_target=_marking_target(objects))
                    n_objects += len(marks)
                    if not marks:
                        print("[tfa] WARNING: no marking set for unit %r" % self.marking_unit)
                    else:
                        print("[tfa] markings: %s -> %d decal(s) using %s"
                              % (minfo.get("unit"), minfo.get("objects"), minfo.get("font")))
                if self.signature_text and objects:
                    sig, sinfo = make_signature(folder, self.signature_text,
                                                height=self.signature_height,
                                                colour=tuple(self.signature_colour),
                                                collection=coll,
                                                parent=objects[0].parent or objects[0],
                                                wrap_target=_marking_target(objects),
                                                font_choice=self.signature_font,
                                                place=self.signature_place,
                                                y_frac=self.signature_along)
                    n_objects += len(sig)
                    print("[tfa] signature %r -> %d decal(s) lines=%s height=%.3f m"
                          % (self.signature_text, len(sig), sinfo.get("lines"),
                             sinfo.get("height", 0.0)))

            for note in MISSING_TEXTURE_REPORT[report_start:]:
                print("[tfa] material without texture: %s" % note)
            msg = "%d file(s) -> %d object(s)" % (n_files, n_objects)
            if roots:
                msg += "; root: " + ", ".join(roots[:4])
            if missing_mods:
                msg += "; %d module(s) not found" % len(missing_mods)
            self.report({"INFO"}, msg)
            return {"FINISHED"}

    def menu_func_import(self, context):
        self.layout.operator(IMPORT_OT_tfa_mesh.bl_idname, text="Task Force Admiral Mesh (.mesh)")

    CLASSES = (IMPORT_OT_tfa_mesh,)

    def register():
        for c in CLASSES:
            bpy.utils.register_class(c)
        bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

    def unregister():
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
        for c in reversed(CLASSES):
            bpy.utils.unregister_class(c)

except ImportError:                                          # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# command line mode:  blender -b -P import_tfa_mesh.py -- <path> [out.blend]
# ---------------------------------------------------------------------------
def _cli():
    import sys
    import bpy
    argv = sys.argv
    if "--" not in argv:
        return
    args = [a for a in argv[argv.index("--") + 1:]]
    if not args:
        print("usage: blender -b -P import_tfa_mesh.py -- <file-or-folder> [out.blend]")
        return
    target = args[0]
    out = args[1] if len(args) > 1 else None

    if os.path.isdir(target):
        root, objects, info = import_model_folder(target)
        print("[tfa] folder '%s' -> %d object(s), root=%s"
              % (os.path.basename(target.rstrip("\\/")), len(objects),
                 root.name if root else "-"))
    else:
        folder = os.path.dirname(target)
        files = filter_mesh_files(
            [os.path.join(folder, f) for f in sorted(os.listdir(folder))
             if f.lower().endswith(".mesh")])
        if target not in files:
            files = [target]
        objects = []
        for p in files:
            try:
                objects.extend(import_mesh_file(p))
            except Exception as exc:                         # noqa: BLE001
                print("[tfa] FAILED %s : %s" % (os.path.basename(p), exc))
        root, info = (group_parts(objects, folder) if objects else (None, {}))
        print("[tfa] '%s' -> %d object(s), root=%s"
              % (os.path.basename(target), len(objects), root.name if root else "-"))
        for o in objects:
            print("[tfa]    %-44s verts=%-7d polys=%-7d mat=%s"
                  % (o.name, len(o.data.vertices), len(o.data.polygons),
                     [m.name for m in o.data.materials]))

    if info:
        print("[tfa] parts file : %s" % (os.path.basename(info["parts_file"])
                                         if info.get("parts_file") else "(none found)"))
        print("[tfa] propellers=%s rudders=%s turrets=%s hidden_variants=%s hidden_helpers=%s"
              % (info.get("props"), info.get("rudders"), info.get("turrets"),
                 len(info.get("hidden_variants", [])), len(info.get("hidden_helpers", []))))
        if info.get("missing_modules"):
            print("[tfa] missing turret modules: %s" % info["missing_modules"])
    if MISSING_TEXTURE_REPORT:
        print("[tfa] materials without texture (%d):" % len(MISSING_TEXTURE_REPORT))
        for note in MISSING_TEXTURE_REPORT:
            print("[tfa]    %s" % note)
    if out:
        bpy.ops.wm.save_as_mainfile(filepath=out)
        print("[tfa] saved", out)


# ---------------------------------------------------------------------------
# unit markings (.mrk): tail codes and ID bands drawn with the game's font
# ---------------------------------------------------------------------------
_MRK_LINE = re.compile(r"^([A-Za-z_]+)(?:\[(\d+)\])?\s*=\s*(.*)$")


def _markings_dir(mesh_dir):
    base = os.path.abspath(mesh_dir)
    if os.path.basename(base) in ("models", "collisions"):
        base = os.path.dirname(base)
    return os.path.join(base, "markings")


def _marking_unit_name(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^\s*name\s*=\s*(\S+)", line)
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return None


def list_marking_units(mesh_dir):
    """Unit names that ship a .mrk marking set for this model."""
    d = _markings_dir(mesh_dir)
    units = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".mrk"):
                unit = _marking_unit_name(os.path.join(d, f))
                if unit:
                    units.append(unit)
    return units


def find_marking_file(mesh_dir, unit):
    d = _markings_dir(mesh_dir)
    if not os.path.isdir(d):
        return None
    for f in sorted(os.listdir(d)):
        if not f.lower().endswith(".mrk"):
            continue
        p = os.path.join(d, f)
        if (_marking_unit_name(p) or "").lower() == (unit or "").lower():
            return p
    return None


def parse_marking_file(path):
    """-> {'unit': str, 'count': int, 'entries': [dict, ...]}"""
    out = {"unit": None, "count": 0, "entries": []}
    if not path or not os.path.isfile(path):
        return out
    with open(path, "rb") as fh:
        raw = fh.read().decode("utf-8", "replace")
    entries = {}
    order = []
    for line in re.split(r"[\r\n\x00]+", raw):
        line = line.strip()
        if not line:
            continue
        m = _MRK_LINE.match(line)
        if not m:
            continue
        key, idx, val = m.group(1), m.group(2), m.group(3).strip()
        if idx is None:
            if key == "name":
                out["unit"] = val
            elif key == "count":
                try:
                    out["count"] = int(float(val))
                except ValueError:
                    pass
            continue
        i = int(idx)
        if i not in entries:
            entries[i] = {"index": i}
            order.append(i)
        entries[i][key] = val
    out["entries"] = [entries[i] for i in sorted(order)]
    return out


def _nums(text, count, default=0.0):
    try:
        vals = [float(v) for v in str(text).replace(",", " ").split()]
    except (TypeError, ValueError):
        vals = []
    while len(vals) < count:
        vals.append(default)
    return vals[:count]


def _fmt_number(fmt, number):
    """'EII-3%2N' with 301 -> 'EII-301'."""
    n = int(number)
    two = "%02d" % (n % 100)
    return fmt.replace("%2N", two).replace("%2d", two).replace("%N", str(n))


def _marking_font(media_roots, choice="ijn"):
    """The game ships both a Japanese tail-code font and a US stencil font.

    Note the IJN one draws Latin letters as katakana-like shapes, so it is only
    usable for squadron codes; for a readable signature pick "usn" or a path.
    """
    files = ["USN_Stencil.ttf", "IJN_TailCode.ttf"] if str(choice).lower() == "usn" \
        else ["IJN_TailCode.ttf", "USN_Stencil.ttf"]
    if choice and os.path.isfile(str(choice)):
        return str(choice)
    for root in media_roots or []:
        for fn in files:
            p = os.path.join(root, "fonts", fn)
            if os.path.isfile(p):
                return p
    return None


def _marking_material(colour):
    import bpy
    key = "TFA_decal_%02X%02X%02X%02X" % tuple(
        int(max(0.0, min(1.0, c)) * 255) for c in colour)
    mat = bpy.data.materials.get(key)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(key)
    mat.use_nodes = True
    nt = mat.node_tree
    for nd in list(nt.nodes):
        nt.nodes.remove(nd)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    _set_input(bsdf, "Base Color", (colour[0], colour[1], colour[2], 1.0))
    _set_input(bsdf, "Roughness", 0.55)
    _set_input(bsdf, "Alpha", colour[3])
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    for attr, val in (("blend_method", "BLEND"), ("surface_render_method", "BLENDED")):
        try:
            setattr(mat, attr, val)
        except Exception:
            pass
    mat.use_backface_culling = False
    mat["tfa_note"] = "unit marking decal (colour from the game's .mrk)"
    return mat


def _font_cap_ratio(font, coll, sample="E"):
    """Measure the cap height per em so `charHeight` from the .mrk is honoured."""
    import bpy
    curve = bpy.data.curves.new("tfa_measure", type="FONT")
    curve.body = sample
    if font:
        curve.font = font
    curve.size = 1.0
    obj = bpy.data.objects.new("tfa_measure", curve)
    coll.objects.link(obj)
    try:
        bpy.context.view_layer.update()
        ys = [c[1] for c in obj.bound_box]
        height = max(ys) - min(ys)
    except Exception:                                        # noqa: BLE001
        height = 0.7
    bpy.data.objects.remove(obj, do_unlink=True)
    return height if 0.05 < height < 3.0 else 0.7


def _wrap_decal(obj, target, cuts=3, offset=0.004, method="PROJECT"):
    """Project a flat text decal onto the hull, so bands wrap the fuselage.

    The game projects markings onto the mesh (the .mrk anchors sit near the
    centre line); a flat quad would float in the air, so convert to a mesh,
    subdivide it and shrinkwrap it. "PROJECT" along the decal's own facing axis
    (inwards only) keeps the letter orientation; "NEAREST" snaps to whatever
    surface is closest and visibly rotates/twists the glyphs.
    """
    import bpy
    try:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.convert(target="MESH")
        if cuts:
            sub = obj.modifiers.new("tfa_subdiv", "SUBSURF")
            sub.subdivision_type = "SIMPLE"
            sub.levels = cuts
            sub.render_levels = cuts
        sw = obj.modifiers.new("tfa_wrap", "SHRINKWRAP")
        sw.target = target
        if str(method).upper().startswith("PROJECT"):
            sw.wrap_method = "PROJECT"
            sw.use_project_z = True
            sw.use_negative_direction = True
            sw.use_positive_direction = False
        else:
            sw.wrap_method = "NEAREST_SURFACEPOINT"
        sw.offset = offset
        obj["tfa_note"] = str(obj.get("tfa_note", "")) + " (projected onto the hull)"
    except Exception:                                        # noqa: BLE001
        pass
    return obj


def _marking_target(objects):
    """The hull mesh the decals should be projected onto."""
    target = None
    for o in objects or []:
        if getattr(o, "type", "") != "MESH":
            continue
        name = str(o.get("tfa_mesh", "")).lower()
        if "fuselage" in name or "body" in name:
            return o
    meshes = [o for o in objects or [] if getattr(o, "type", "") == "MESH"]
    if meshes:
        target = max(meshes, key=lambda o: len(o.data.vertices))
    return target


def make_markings(mesh_dir, unit, number=301, media_roots=None, collection=None,
                  parent=None, z_up=True, char_ratio=None, lift=0.02,
                  wrap=True, wrap_target=None, wrap_cuts=3):
    """Build Blender text decals for one unit's .mrk marking set."""
    import bpy
    from mathutils import Euler, Matrix, Vector

    if media_roots is None:
        media_roots = find_media_roots(mesh_dir)
    path = find_marking_file(mesh_dir, unit)
    if not path:
        return [], {"unit": unit, "file": None, "objects": 0,
                    "available": list_marking_units(mesh_dir)}
    data = parse_marking_file(path)
    font_path = _marking_font(media_roots)
    font = None
    if font_path:
        font = bpy.data.fonts.get(os.path.basename(font_path)) or bpy.data.fonts.load(font_path)

    conv = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
    coll = collection if collection is not None else bpy.context.scene.collection
    if char_ratio is None:
        char_ratio = _font_cap_ratio(font, coll)

    made = []
    for e in data["entries"]:
        fmt = e.get("format", "")
        if not fmt:
            continue
        body = _fmt_number(fmt, number)
        curve = bpy.data.curves.new("decal_%d" % e["index"], type="FONT")
        curve.body = body
        if font:
            curve.font = font
        char_h = abs(_nums(e.get("charHeight"), 1, 0.2)[0]) or 0.2
        curve.size = char_h / max(0.1, char_ratio)
        curve.align_x = {"left": "LEFT", "center": "CENTER",
                         "right": "RIGHT"}.get(str(e.get("alignment", "center")).lower(),
                                               "CENTER")
        curve.align_y = "CENTER"
        obj = bpy.data.objects.new("decal_%s_%02d" % (data["unit"] or unit, e["index"]), curve)
        obj["tfa_control"] = "marking"
        obj["tfa_note"] = "%s: %s" % (data["unit"] or unit, body)

        pos = conv @ Vector(_nums(e.get("position"), 3))
        rot = _nums(e.get("orientation"), 3)
        rm = Euler((math.radians(rot[0]), math.radians(rot[1]), math.radians(rot[2])),
                   "XYZ").to_matrix().to_4x4()
        # the game's decal frame equals Blender's text frame (lying in local XY,
        # facing local +Z, up local +Y), so the rotation only needs the axis
        # conversion - conjugating it as well made the text lie flat on the deck
        rb = conv @ rm
        obj.rotation_euler = rb.to_euler()
        normal = obj.rotation_euler.to_matrix() @ Vector((0.0, 0.0, 1.0))
        obj.location = pos + normal * lift
        stretch = _nums(e.get("stretch"), 1, 1.0)[0] or 1.0
        obj.scale = (abs(stretch), 1.0, 1.0)
        obj.data.materials.append(_marking_material(_nums(e.get("colour"), 4, 1.0)))
        if parent is not None:
            obj.parent = parent
        coll.objects.link(obj)
        if wrap and wrap_target is not None:
            _wrap_decal(obj, wrap_target, cuts=wrap_cuts)
        made.append(obj)

    info = {"unit": data["unit"], "file": os.path.basename(path), "objects": len(made),
            "font": os.path.basename(font_path) if font_path else None,
            "char_ratio": round(char_ratio, 3),
            "wrapped": bool(wrap and wrap_target is not None),
            "available": list_marking_units(mesh_dir)}
    return made, info


def _split_lines(text, count):
    """Split a string into `count` lines, preferring camelCase/space boundaries."""
    text = str(text or "").strip()
    if count <= 1 or not text:
        return [text]
    words = text.split()
    if len(words) >= count:
        lines = [""] * count
        for w in sorted(words, key=len, reverse=True):
            i = min(range(count), key=lambda k: len(lines[k]))
            lines[i] = (lines[i] + " " + w).strip()
        return lines
    # candidate break points: after a space/dash, or at a camelCase boundary
    cuts = [i for i in range(1, len(text))
            if text[i - 1] in " -_" or (text[i].isupper() and text[i - 1].islower())]
    lines = []
    rest = text
    for remaining in range(count, 1, -1):
        target = len(rest) / float(remaining)
        if cuts:
            best = min(cuts, key=lambda c: abs(c - target))
            lines.append(rest[:best].strip(" -_"))
            rest = rest[best:].strip(" -_")
            cuts = [c - best for c in cuts if c > best]
        else:
            step = max(1, int(round(target)))
            lines.append(rest[:step])
            rest = rest[step:]
    lines.append(rest)
    return [ln for ln in lines if ln]


def _tail_code_anchors(mesh_dir, unit=None):
    """Positions/orientations of the tail-code decals, used to place a signature."""
    units = list_marking_units(mesh_dir)
    if not units:
        return []
    for cand in ([unit] if unit else []) + units:
        data = parse_marking_file(find_marking_file(mesh_dir, cand))
        anchors = [e for e in data["entries"]
                   if "%2N" in str(e.get("format", "")) or "%N" in str(e.get("format", ""))]
        if anchors:
            return anchors
    return []


def _fin_anchor(hull_obj, y_frac=0.78, z_frac=0.60):
    """Anchor + usable width for the vertical tail (measured from the hull)."""
    if hull_obj is None or getattr(hull_obj, "type", "") != "MESH":
        return None
    vs = [hull_obj.matrix_world @ v.co for v in hull_obj.data.vertices]
    if not vs:
        return None
    ys = [v.y for v in vs]
    y_lo = min(ys) + max(0.5, min(0.95, float(y_frac))) * (max(ys) - min(ys))
    sel = [v for v in vs if v.y >= y_lo]
    if not sel:
        return None
    z_lo = min(v.z for v in sel)
    z_hi = max(v.z for v in sel)
    z = z_lo + max(0.1, min(0.9, float(z_frac))) * (z_hi - z_lo)
    band = [v for v in sel if abs(v.z - z) < 0.12] or sel
    y0 = min(v.y for v in band)
    y1 = max(v.y for v in band)
    x = max(abs(v.x) for v in sel) or 0.05
    return {"pos": (x, 0.5 * (y0 + y1), z),
            "chord": max(0.25, y1 - y0),
            "height": max(0.15, z_hi - z_lo)}


def _hull_side_anchor(hull_obj, y_frac=0.80, band=0.45):
    """A point on the fuselage flank near the tail: (skin |x|, y, mid z)."""
    if hull_obj is None or getattr(hull_obj, "type", "") != "MESH":
        return None
    vs = [hull_obj.matrix_world @ v.co for v in hull_obj.data.vertices]
    if not vs:
        return None
    ys = [v.y for v in vs]
    ymin, ymax = min(ys), max(ys)
    y = ymin + max(0.05, min(0.95, float(y_frac))) * (ymax - ymin)
    sel = [v for v in vs if abs(v.y - y) < band] or vs
    # use the widest point of the whole hull for the decal plane: the fuselage
    # tapers, so a plane sized on the local radius sinks into the fatter part
    # ahead of it. The inward projection then snaps it back onto the skin.
    x = max(abs(v.x) for v in vs) or 0.2
    zs = [v.z for v in sel]
    return (x, y, 0.5 * (min(zs) + max(zs)))


def make_signature(mesh_dir, text, height=0.17, max_width=1.15, colour=(0.92, 0.92, 0.90, 1.0),
                   unit=None, media_roots=None, collection=None, parent=None,
                   max_lines=2, wrap_target=None, wrap_cuts=3, lift=0.006, font_choice="usn",
                   place="fin", y_frac=0.68):
    """Put a custom string (a signature / watermark) on both sides of the tail.

    ``place="fuselage"`` centres it on the rear fuselage flank (room for a long
    name); ``place="fin"`` puts it where the game's tail code goes. Anchors,
    orientation and the wrap come from the game's own data.
    """
    import bpy
    from mathutils import Euler, Matrix, Vector

    text = str(text or "").strip()
    if not text:
        return [], {"objects": 0}
    if media_roots is None:
        media_roots = find_media_roots(mesh_dir)
    anchors = _tail_code_anchors(mesh_dir, unit)
    font_path = _marking_font(media_roots, font_choice)
    font = None
    if font_path:
        font = bpy.data.fonts.get(os.path.basename(font_path)) or bpy.data.fonts.load(font_path)
    conv = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))
    coll = collection if collection is not None else bpy.context.scene.collection
    ratio = _font_cap_ratio(font, coll)

    def build(body, size):
        curve = bpy.data.curves.new("sig", type="FONT")
        curve.body = body
        if font:
            curve.font = font
        curve.size = size / max(0.1, ratio)
        curve.align_x = "RIGHT"
        curve.align_y = "CENTER"
        obj = bpy.data.objects.new("signature", curve)
        coll.objects.link(obj)
        bpy.context.view_layer.update()
        xs = [c[0] for c in obj.bound_box]
        ys = [c[1] for c in obj.bound_box]
        return obj, (max(xs) - min(xs)), (max(ys) - min(ys))

    lines = [text]
    probe, width, _h = build(text, height)
    bpy.data.objects.remove(probe, do_unlink=True)
    if width > max_width and max_lines > 1:
        for n in range(2, max_lines + 1):
            cand = _split_lines(text, n)
            probe, width, _h = build("\n".join(cand), height)
            bpy.data.objects.remove(probe, do_unlink=True)
            if width <= max_width:
                lines = cand
                break
    size = height
    probe, width, _h = build("\n".join(lines), size)
    bpy.data.objects.remove(probe, do_unlink=True)
    if width > max_width:
        size = height * (max_width / width)

    side_anchor = None
    fin_anchor = None
    if str(place).lower() == "fin":
        fin_anchor = _fin_anchor(wrap_target)
        if fin_anchor is not None:
            max_width = min(max_width, fin_anchor["chord"] * 0.80)
            height = min(height, fin_anchor["height"] * 0.42)
    else:
        side_anchor = _hull_side_anchor(wrap_target, y_frac=y_frac)
    spots = []
    if fin_anchor is not None:
        sx, sy, sz = fin_anchor["pos"]
        for sgn in (1.0, -1.0):
            spots.append((Vector((sx * sgn, sy, sz)),
                          Euler((math.radians(90.0), 0.0, math.radians(90.0 * sgn)),
                                "XYZ")))
    elif side_anchor is not None:
        sx, sy, sz = side_anchor
        for sgn in (1.0, -1.0):
            spots.append((Vector((sx * sgn, sy, sz)),
                          Euler((math.radians(90.0), 0.0, math.radians(90.0 * sgn)),
                                "XYZ")))
    else:
        for anchor in anchors[:2]:
            pos = conv @ Vector(_nums(anchor.get("position"), 3))
            rot = _nums(anchor.get("orientation"), 3)
            rm = Euler((math.radians(rot[0]), math.radians(rot[1]), math.radians(rot[2])),
                       "XYZ").to_matrix().to_4x4()
            spots.append((pos, (conv @ rm).to_euler()))
    align = "RIGHT" if (side_anchor is None and fin_anchor is None) else "CENTER"

    made = []
    for side, (pos, rb) in enumerate(spots):
        up = rb.to_matrix() @ Vector((0.0, 1.0, 0.0))
        normal = rb.to_matrix() @ Vector((0.0, 0.0, 1.0))
        step = size * 1.35
        for li, line in enumerate(lines):
            if not line.strip():
                continue
            curve = bpy.data.curves.new("sig_%d_%d" % (side, li), type="FONT")
            curve.body = line
            if font:
                curve.font = font
            curve.size = size / max(0.1, ratio)
            curve.align_x = align
            curve.align_y = "CENTER"
            obj = bpy.data.objects.new("signature_%d_%d" % (side, li), curve)
            obj["tfa_control"] = "signature"
            obj["tfa_note"] = "signature text: %s" % line
            obj.rotation_euler = rb
            offset = ((len(lines) - 1) / 2.0 - li) * step
            obj.location = pos + up * offset + normal * lift
            mat = _marking_material(tuple(colour))
            obj.data.materials.append(mat)
            if parent is not None:
                obj.parent = parent
            coll.objects.link(obj)
            if wrap_target is not None:
                _wrap_decal(obj, wrap_target, cuts=wrap_cuts, offset=0.004)
            made.append(obj)

    info = {"objects": len(made), "text": text, "lines": lines, "height": round(size, 3),
            "font": os.path.basename(font_path) if font_path else None,
            "anchors": len(anchors)}
    return made, info


if __name__ == "__main__":
    _cli()

