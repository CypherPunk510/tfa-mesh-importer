# TFA Mesh Importer for Blender

Read **Task Force Admiral** `.mesh` files (OGRE `MeshSerializer_v1.100`) directly in Blender —
no external converter, no round-trip through Assimp or OgreXML.

The importer parses the binary format itself, rebuilds submeshes with their UVs and normals,
resolves the game's materials and textures, and wires up a few game-specific extras
(see *What it handles*).

> **Status:** personal tooling, published in case it saves someone else the reverse-engineering.
> It targets a specific retail build of the game; other versions may need tweaks.

## ⚠️ Usage notice — learning and research only

This project was written to **study how models are made**: to learn from a commercial
engine's asset format, and to understand mesh construction by taking one apart.

* **Commercial use is not permitted.** You may not sell this software, bundle it into a
  paid product or service, or use it for paid client work.
* **For personal study, hobby projects, research and education** — go ahead.
* **No game assets are included**, and none may be redistributed. Everything in this
  repository is code; the game's models, textures and data remain the property of their
  rights holders.
* If you fork or reuse it, keep this notice and the same terms.

The full terms are in [LICENSE](LICENSE).

---

## What it handles

* **OGRE binary `.mesh`** — chunked container, vertex declarations, index/vertex buffers
* **Submeshes** — separate objects with their own material slots
* **Materials** — parsed from the game's material files, mapped to Blender materials
* **Textures** — resolved and imported; textures shipped as `.dds` are handled
* **LODs** — lower-detail meshes are imported and tagged so you can pick a level
* **Vertex colours** and packed colour formats
* **Propeller blur discs** — generated procedurally, since the game ships sprites for them
* **Engine helper meshes** — imported but hidden by default
* **Signatures / markings** — text decals, including wrapping onto the hull surface
* **Missing-texture report** — printed at the end, so you know what needs manual work

## Requirements

| | |
|---|---|
| Blender | 3.0+ declared in `bl_info`; developed and tested on **5.1** |
| Python | only the standard library plus `numpy` (both ship with Blender) |

## Install as an add-on

1. Copy `import_tfa_mesh.py` into your Blender add-on folder:
   * Windows: `%APPDATA%\Blender Foundation\Blender\<version>\scripts\addons\`
   * or use **Edit → Preferences → Add-ons → Install…** and pick the file
2. Enable **“Task Force Admiral OGRE Mesh (.mesh)”** in Preferences → Add-ons
3. **File → Import → Task Force Admiral Mesh (.mesh)**

## Use as a batch script

```bash
blender --background --python import_tfa_mesh.py -- <file-or-folder> [out.blend]
```

Point it at a single `.mesh` or at a folder to convert a batch into one `.blend`.

## Format notes

The reverse-engineered layout — chunk sizes, string termination, vertex element layout —
is documented in [`docs/ogre-mesh-format.md`](docs/ogre-mesh-format.md).
If you are writing your own importer, read that first; those were the details that
made the difference between “plausible” and “actually loads”.

## Limitations

* Skeletons/animations are not reconstructed (static geometry only)
* Material shaders are not recreated — you get geometry, UVs, materials and textures,
  not the game's render pipeline
* Texture lookups rely on the game's folder layout; a heavily modded install may need paths fixed
* Numeric conventions (axis conversion, scale) are tuned for the game's units

## Legal

This repository contains **code only**.

* No game assets are included — no `.mesh`, no textures, no `.dds`.
* The importer is meant for **interoperability with files you already own** — modding,
  personal research, video work with assets from your own installation.
* *Task Force Admiral* and all of its content remain the property of their respective
  rights holders. This project is not affiliated with or endorsed by them.

If a rights holder objects to a format importer being public, the polite thing is to take it
down — that is a decision for the maintainer, not a licence permission.

## License

**Non-commercial** — study, research, hobby and education only; commercial use is not
permitted. See [LICENSE](LICENSE) for the full terms.

> This is a concise, plain-language licence written for this project, not a
> lawyer-drafted one. If you want a standard text instead, [PolyForm
> Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) covers
> roughly the same ground.

---

## 中文说明

**用途**：在 Blender 里直接读取《Task Force Admiral》的 `.mesh` 文件（OGRE 二进制格式），
不需要任何外部转换工具。

**安装**：把 `import_tfa_mesh.py` 放进 Blender 的 `scripts/addons/` 目录 →
偏好设置 → 插件 → 启用 “Task Force Admiral OGRE Mesh (.mesh)” →
文件 → 导入 → Task Force Admiral Mesh。

**批量转换**：

```bash
blender --background --python import_tfa_mesh.py -- <文件或文件夹> [输出.blend]
```

**能读的东西**：子网格、材质、贴图（含 `.dds`）、LOD、顶点色、螺旋桨动态模糊盘、
引擎辅助网格（默认隐藏）、签名/标识贴花（可贴合船体）、缺失贴图报告。

**读不了的东西**：骨骼动画（只出静态几何）、游戏里的着色器效果。

**使用声明**：这个脚本是**为了学习模型的制作方法**（研究商业引擎怎么存模型、了解网格格式）而写的 ✓
**禁止商用** ✓ —— 不得出售、不得打包进付费产品或服务、不得用于收费项目 ✓。
个人学习、爱好项目、研究与教学可以自由使用 ✓。
如果 fork 或二次使用，请保留这份声明与相同条款 ✓。

**版权**：仓库里**只有代码**，不含任何游戏资源文件 ✓。请只对你自己合法拥有的安装使用它 ✓
游戏的全部内容归其权利方所有 ✓ 本项目与其无关联、未获其背书 ✓。

完整条款见 [LICENSE](LICENSE) ✓。
