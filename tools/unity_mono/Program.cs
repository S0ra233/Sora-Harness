// Static assembly editing only. No game assemblies are executed or reflected into the runtime.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Web.Script.Serialization;
using dnlib.DotNet;
using dnlib.DotNet.Emit;
using dnlib.DotNet.Writer;

public sealed class Change {
    public string path { get; set; }
    public string original { get; set; }
    public string translation { get; set; }
}

public sealed class Request {
    public string operation { get; set; }
    public string path { get; set; }
    public string data { get; set; }
    public Change[] changes { get; set; }
}

public static class Program {
    static string Location(MethodDef method, Instruction instruction) {
        return "/methods/" + method.MDToken.Raw.ToString("X8") + "/il/" + instruction.Offset.ToString("X8");
    }

    static Dictionary<string, object> Inspect(ModuleDefMD module) {
        var literals = new List<object>();
        foreach (var type in module.GetTypes()) {
            foreach (var method in type.Methods) {
                if (!method.HasBody) continue;
                foreach (var instruction in method.Body.Instructions) {
                    if (instruction.OpCode.Code != Code.Ldstr) continue;
                    literals.Add(new {
                        path = Location(method, instruction), text = (string)instruction.Operand,
                        type_name = type.FullName, method_name = method.FullName
                    });
                }
            }
        }
        bool signed = module.IsStrongNameSigned || (module.Assembly != null && module.Assembly.HasPublicKey);
        return new Dictionary<string, object> {
            { "mvid", module.Mvid.ToString() }, { "assembly", module.FullName },
            { "write_supported", module.IsILOnly && !signed },
            { "write_limitation", signed ? "Strong-name assembly requires a separate signing plan" :
                (!module.IsILOnly ? "Mixed-mode assembly is not supported for writing" : "") },
            { "literals", literals }
        };
    }

    static object Process(Request request) {
        byte[] raw = request.data != null ? Convert.FromBase64String(request.data) : File.ReadAllBytes(request.path);
        var options = new ModuleCreationOptions { TryToLoadPdbFromDisk = false };
        using (var module = ModuleDefMD.Load(raw, options)) {
            var inventory = Inspect(module);
            if (request.operation == "inspect") return inventory;
            if (request.operation != "write") throw new InvalidDataException("Unknown operation");
            if (!(bool)inventory["write_supported"]) throw new InvalidDataException((string)inventory["write_limitation"]);
            var locations = new Dictionary<string, Instruction>();
            foreach (var type in module.GetTypes()) {
                foreach (var method in type.Methods) {
                    if (!method.HasBody) continue;
                    method.Body.KeepOldMaxStack = true;
                    foreach (var instruction in method.Body.Instructions) {
                        if (instruction.OpCode.Code == Code.Ldstr) locations.Add(Location(method, instruction), instruction);
                    }
                }
            }
            if (request.changes == null || request.changes.Length == 0) throw new InvalidDataException("No changes supplied");
            var seen = new HashSet<string>();
            foreach (var change in request.changes) {
                Instruction instruction;
                if (!seen.Add(change.path) || !locations.TryGetValue(change.path, out instruction))
                    throw new InvalidDataException("Duplicate or missing instruction: " + change.path);
                if ((string)instruction.Operand != change.original || change.translation == null)
                    throw new InvalidDataException("Original string changed: " + change.path);
                instruction.Operand = change.translation;
            }
            var writer = new ModuleWriterOptions(module);
            writer.MetadataOptions.Flags |= MetadataFlags.PreserveAll;
            writer.WritePdb = false;
            using (var stream = new MemoryStream()) {
                module.Write(stream, writer);
                return new { data = Convert.ToBase64String(stream.ToArray()), written = seen.Count };
            }
        }
    }

    public static int Main() {
        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        var json = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue };
        try {
            var request = json.Deserialize<Request>(Console.In.ReadToEnd());
            Console.Write(json.Serialize(Process(request)));
            return 0;
        } catch (Exception error) {
            Console.Error.Write(error.GetType().Name + ": " + error.Message);
            return 1;
        }
    }
}
