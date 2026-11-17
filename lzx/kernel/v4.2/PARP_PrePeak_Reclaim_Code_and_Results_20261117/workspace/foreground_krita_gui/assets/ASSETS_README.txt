# 素材放置说明

本目录需要放置 Krita 前台操作素材文件：

- 文件名: krita_14gib_4096x4096_224layers.ora
- 规格: 4096×4096 像素, 224 图层
- 预期 SHA-256: dd04ddf49d28690527dbc85d879981af3ea9ed62f1a3ce37053e453fef6e09bf

素材放置后，在 configs/gui.json 中更新 asset.path 和 asset.sha256，
或者在目标机器上运行:
  python3 -m oomlab_krita_gui.cli inspect
  python3 -m oomlab_krita_gui.cli inspect --select-config configs/gui.selected.json
