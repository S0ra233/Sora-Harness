using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using AddressablesTools;
using AddressablesTools.Catalog;
using AddressablesTools.Classes;

// Reads data only. No game assemblies are executed and no files are overwritten.
public static class Program
{
    static readonly JsonSerializerOptions Json = new() { IncludeFields = true, MaxDepth = 128 };
    public sealed class Change
    {
        public string internal_id { get; set; }
        public uint crc { get; set; }
        public long size { get; set; }
    }
    public sealed class Request
    {
        public string path { get; set; }
        public string operation { get; set; }
        public Change[] changes { get; set; } = Array.Empty<Change>();
    }
    static ContentCatalogData Read(byte[] bytes, bool binary) => binary
        ? AddressablesCatalogFileParser.FromBinaryData(bytes)
        : AddressablesCatalogFileParser.FromJsonString(Encoding.UTF8.GetString(bytes).TrimStart('\uFEFF'));

    static object Location(ResourceLocation r, HashSet<ResourceLocation> chain)
    {
        if (!chain.Add(r)) throw new InvalidDataException("Cyclic catalog dependency is unsupported");
        var children = r.Dependencies?.Select(d => Location(d, chain)).ToArray();
        chain.Remove(r);
        return new { r.InternalId, r.ProviderId, r.PrimaryKey, r.DependencyKey,
            r.DependencyHashCode, r.Type, r.Data, Dependencies = children };
    }
    static string Snapshot(ContentCatalogData c) => JsonSerializer.Serialize(new {
        c.Version, c.LocatorId, c.BuildResultHash, c.InstanceProviderData,
        c.SceneProviderData, c.ResourceProviderData,
        Resources = c.Resources.Select(pair => new {
            KeyType = pair.Key.GetType().FullName, Key = pair.Key,
            Locations = pair.Value.Select(r => Location(r, new())).ToArray()
        }).OrderBy(x => x.KeyType + JsonSerializer.Serialize(x.Key, Json)).ToArray()
    }, Json);

    static IEnumerable<ResourceLocation> All(ContentCatalogData c)
    {
        var pending = new Stack<ResourceLocation>(c.Resources.Values.SelectMany(x => x));
        var visited = new HashSet<ResourceLocation>();
        while (pending.Count > 0)
        {
            var r = pending.Pop();
            if (!visited.Add(r)) continue;
            yield return r;
            if (r.Dependencies != null) foreach (var d in r.Dependencies) pending.Push(d);
        }
    }
    public static int Main()
    {
        try
        {
            var request = JsonSerializer.Deserialize<Request>(Console.In.ReadToEnd(), Json);
            var raw = File.ReadAllBytes(request.path);
            bool binary = raw.Length >= 8 && BitConverter.ToUInt32(raw, 0) == 0x0de38942;
            // v2 is the version checked with our game samples. Do not silently convert versions.
            if (binary && BitConverter.ToUInt32(raw, 4) != 2)
                throw new NotSupportedException("Only binary catalog v2 is enabled");
            var catalog = Read(raw, binary);
            var locations = All(catalog).ToArray();
            var bundles = locations.Where(r => r.Data is WrappedSerializedObject { Object: AssetBundleRequestOptions })
                .Select(r => {
                    var options = (AssetBundleRequestOptions)((WrappedSerializedObject)r.Data).Object;
                    return new { internal_id = r.InternalId, provider = r.ProviderId,
                        crc = options.Crc, hash = options.Hash, size = options.BundleSize,
                        name = options.BundleName, use_web_request = options.ComInfo.UseUnityWebRequestForLocalBundles };
                }).Distinct().OrderBy(x => x.internal_id).ToArray();
            string data = null;
            if (request.operation == "rewrite")
            {
                var changes = request.changes.ToDictionary(c => c.internal_id);
                var found = new HashSet<string>();
                foreach (var r in locations)
                {
                    if (!changes.TryGetValue(r.InternalId, out var change)) continue;
                    if (r.Data is not WrappedSerializedObject { Object: AssetBundleRequestOptions options })
                        throw new InvalidDataException("Requested location is not a bundle");
                    if (change.size < 0 || change.size > uint.MaxValue)
                        throw new InvalidDataException("Bundle size outside supported range");
                    options.Crc = change.crc;
                    options.BundleSize = change.size;
                    found.Add(r.InternalId);
                }
                if (found.Count != changes.Count) throw new InvalidDataException("Bundle location missing");
                var expected = Snapshot(catalog);
                byte[] result = binary ? AddressablesCatalogFileParser.ToBinaryData(catalog)
                    : Encoding.UTF8.GetBytes(AddressablesCatalogFileParser.ToJsonString(catalog));
                if (Snapshot(Read(result, binary)) != expected)
                    throw new InvalidDataException("Catalog roundtrip changed resource keys, dependencies or data");
                data = Convert.ToBase64String(result);
            }
            else if (request.operation != "inspect") throw new ArgumentException("Unknown operation");
            Console.Write(JsonSerializer.Serialize(new { format = binary ? "binary_v2" : "json",
                keys = catalog.Resources.Count, bundles, data, roundtrip_checked = data != null }, Json));
            return 0;
        }
        catch (Exception ex) { Console.Error.Write(ex.ToString()); return 1; }
    }
}
