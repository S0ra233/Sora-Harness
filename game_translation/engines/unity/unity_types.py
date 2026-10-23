"""Strict component parsing; recover missing layouts from this game's assemblies."""
from pathlib import Path


TMP_CLASSES = {"TMPro.TextMeshPro", "TMPro.TextMeshProUGUI"}


class ComponentReader:
    def __init__(self, data_dir):
        self.data = Path(data_dir)
        self.generator = None
        self.nodes = {}
        self.failures = {}
        self.dependencies = set()
        self.load_error = None

    @classmethod
    def for_asset(cls, path):
        for parent in Path(path).resolve().parents:
            if parent.name.lower().endswith("_data") or (parent / "globalgamemanagers").is_file():
                return cls(parent)
        raise ValueError("无法定位组件所属游戏数据目录；需保留资源与程序集的目录关系")

    def identify(self, obj):
        if obj.type.name != "MonoBehaviour":
            return None
        # Only the header is read here; it is never treated as complete text data.
        head = obj.parse_monobehaviour_head()
        script_obj = head.m_Script.deref()
        script = script_obj.read()
        fullname = ".".join(x for x in (script.m_Namespace, script.m_ClassName) if x)
        script_file = self.data / str(script_obj.assets_file.name)
        if script_file.is_file():
            self.dependencies.add(script_file)
        return {"assembly": script.m_AssemblyName, "class": fullname}

    def _generate(self, obj, identity):
        from UnityPy.helpers.TypeTreeNode import TypeTreeNode
        key = (obj.assets_file.unity_version, identity["assembly"], identity["class"])
        if key in self.failures:
            raise ValueError(self.failures[key])
        if key in self.nodes:
            return self.nodes[key]
        try:
            if self.load_error:
                raise ValueError(self.load_error)
            if self.generator is None:
                try:
                    from TypeTreeGeneratorAPI import TypeTreeGenerator
                    generator = TypeTreeGenerator(obj.assets_file.unity_version)
                    il2cpp = self.data.parent / "GameAssembly.dll"
                    metadata = self.data / "il2cpp_data/Metadata/global-metadata.dat"
                    if il2cpp.is_file() and metadata.is_file():
                        dependencies = [il2cpp, metadata]
                        generator.load_il2cpp(il2cpp.read_bytes(), metadata.read_bytes())
                    else:
                        dependencies = sorted((self.data / "Managed").glob("*.dll"))
                        if not dependencies:
                            raise ValueError("找不到本游戏的 Managed DLL 或 IL2CPP 类型信息")
                        for path in dependencies:
                            generator.load_dll(path.read_bytes())
                    self.dependencies.update(dependencies)
                    self.generator = generator
                except Exception as exc:
                    self.load_error = f"组件类型恢复失败（需要 TypeTreeGeneratorAPI 和游戏类型文件）: {exc}"
                    raise ValueError(self.load_error) from exc
            # IL2CPP imports expose assembly names without .dll; Mono may retain it.
            names = self.generator.get_loaded_dll_names()
            matches = [name for name in names if name.removesuffix(".dll") == identity["assembly"].removesuffix(".dll")]
            if len(matches) != 1:
                raise ValueError("组件程序集定位不唯一或未加载")
            nodes = self.generator.get_nodes(matches[0], identity["class"])
            node = TypeTreeNode.from_list([TypeTreeNode(
                n.m_Level, n.m_Type, n.m_Name, 0, 0, m_MetaFlag=n.m_MetaFlag) for n in nodes])
            self.nodes[key] = node
            return node
        except Exception as exc:
            self.failures[key] = f"{identity['class']} 类型恢复失败: {exc}"
            raise ValueError(self.failures[key]) from exc

    def read(self, obj, identity):
        try:
            tree = obj.read_typetree()
            node = None
        except ValueError:
            if identity is None:
                raise ValueError("对象缺少类型结构，且无法定位脚本类型")
            node = self._generate(obj, identity)
            tree = obj.read_typetree(nodes=node)
        return tree, node


class TMPReader(ComponentReader):
    """Compatibility entry for local tools; built-in routes use ComponentReader.

    Keep the older TMP-only behavior available until external callers are known.
    """

    def identify(self, obj):
        identity = super().identify(obj)
        return identity if identity and identity["class"] in TMP_CLASSES else None

    def read(self, obj, identity):
        tree, node = super().read(obj, identity)
        if not isinstance(tree.get("m_text"), str):
            raise ValueError("完整 TMP 对象中没有字符串 m_text 字段")
        return tree, node
