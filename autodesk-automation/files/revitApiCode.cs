using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using Autodesk.Revit.ApplicationServices;
using Autodesk.Revit.DB;
using Autodesk.Revit.DB.Structure;
using DesignAutomationFramework;

namespace MyRevitAddin
{
    [Autodesk.Revit.Attributes.Regeneration(Autodesk.Revit.Attributes.RegenerationOption.Manual)]
    [Autodesk.Revit.Attributes.Transaction(Autodesk.Revit.Attributes.TransactionMode.Manual)]
    public class BuildStructureApp : IExternalDBApplication
    {
        private const string JsonLocalName = "structure.json";
        private const string DefaultUnits = "m"; // m, mm, ft

        private const double FeetPerM = 3.280839895013123;
        private const double FeetPerMm = FeetPerM / 1000.0;
        private const double SegmentTolFt = 1e-4;

        public ExternalDBApplicationResult OnStartup(ControlledApplication app)
        {
            DesignAutomationBridge.DesignAutomationReadyEvent += OnDesignAutomationReady;
            return ExternalDBApplicationResult.Succeeded;
        }

        public ExternalDBApplicationResult OnShutdown(ControlledApplication app)
        {
            return ExternalDBApplicationResult.Succeeded;
        }

        private void OnDesignAutomationReady(object sender, DesignAutomationReadyEventArgs e)
        {
            try
            {
                Run(e.DesignAutomationData);
                e.Succeeded = true;
            }
            catch (Exception ex)
            {
                Console.WriteLine("DA: Failed with exception, " + ex);
                e.Succeeded = false;
            }
        }

        private static void Run(DesignAutomationData data)
        {
            if (data == null) throw new ArgumentNullException(nameof(data));
            Application rvtApp = data.RevitApp ?? throw new InvalidOperationException("RevitApp is null");
            Document doc = data.RevitDoc ?? throw new InvalidOperationException("RevitDoc is null");

            string cwd = Directory.GetCurrentDirectory();
            string jsonPath = Path.Combine(cwd, JsonLocalName);
            Console.WriteLine("DA: Working directory, " + cwd);
            Console.WriteLine("DA: Expecting JSON at, " + jsonPath);

            StructureInput input = ReadInput(jsonPath);
            if (input == null || input.Connectivity == null || input.Connectivity.Count == 0 ||
                input.Lines == null || input.Lines.Count == 0)
            {
                Console.WriteLine("DA: Input has no connectivity or lines, nothing to do");
                Save(doc);
                return;
            }

            var builder = new SimpleBuilder(doc, input.Units ?? DefaultUnits);
            var summary = builder.CreateMembers(input.Connectivity, input.Lines);

            Console.WriteLine($"DA: Created, {summary.Created}, Skipped, {summary.Skipped}, Errors, {summary.Errors}");
            Save(doc);
        }

        private static void Save(Document doc)
        {
            ModelPath outPath = ModelPathUtils.ConvertUserVisiblePathToModelPath("result.rvt");
            var sao = new SaveAsOptions();
            doc.SaveAs(outPath, sao);
            Console.WriteLine("DA: Saved, result.rvt");
        }

        // ---------------- JSON DTOs ----------------

        [DataContract]
        private class StructureInput
        {
            [DataMember(Name = "units", IsRequired = false)]
            public string Units { get; set; } = DefaultUnits;

            [DataMember(Name = "connectivity", IsRequired = true)]
            public Dictionary<string, Node> Connectivity { get; set; }

            [DataMember(Name = "lines", IsRequired = true)]
            public Dictionary<string, LineDef> Lines { get; set; }
        }

        [DataContract]
        private class Node
        {
            [DataMember(Name = "x")] public double X { get; set; }
            [DataMember(Name = "y")] public double Y { get; set; }
            [DataMember(Name = "z")] public double Z { get; set; }
        }

        [DataContract]
        private class LineDef
        {
            [DataMember(Name = "nodeI")] public string NodeI { get; set; }
            [DataMember(Name = "nodeJ")] public string NodeJ { get; set; }
            [DataMember(Name = "family", IsRequired = false)] public string Family { get; set; }
            [DataMember(Name = "section", IsRequired = false)] public string Section { get; set; }
        }

        private static StructureInput ReadInput(string path)
        {
            if (!File.Exists(path))
            {
                Console.WriteLine("DA: structure.json not found");
                return null;
            }

            try
            {
                string json = File.ReadAllText(path);
                var settings = new DataContractJsonSerializerSettings { UseSimpleDictionaryFormat = true };
                using (var ms = new MemoryStream(Encoding.UTF8.GetBytes(json)))
                {
                    var ser = new DataContractJsonSerializer(typeof(StructureInput), settings);
                    var obj = ser.ReadObject(ms) as StructureInput;
                    Console.WriteLine("DA: Parsed structure.json");
                    return obj;
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine("DA: Could not parse structure.json, " + ex.Message);
                return null;
            }
        }

        // ---------------- Builder with duplicate prevention ----------------

        private class SimpleBuilder
        {
            private readonly Document _doc;
            private readonly string _units;

            private HashSet<string> _existingKeys;

            private readonly List<FamilySymbol> _framingSymbols;

            public SimpleBuilder(Document doc, string units)
            {
                _doc = doc;
                _units = string.IsNullOrWhiteSpace(units) ? DefaultUnits : units.Trim().ToLowerInvariant();

                _framingSymbols = GetSymbols(doc, BuiltInCategory.OST_StructuralFraming);
            }

            public (int Created, int Skipped, int Errors) CreateMembers(
                Dictionary<string, Node> nodes,
                Dictionary<string, LineDef> members)
            {
                int created = 0, skipped = 0, errors = 0;

                _existingKeys = CollectExistingFramingSegmentKeys();

                var seenInputKeys = new HashSet<string>();
                var orderedMembers = members.Values.ToList();

                var newFraming = new List<FamilyInstance>();

                using (var t = new Transaction(_doc, "Build structure"))
                {
                    t.Start();

                    // Get lowest existing level, or create one near the lowest node Z if none exist.
                    Level level = GetOrCreateLevel(nodes);

                    foreach (var m in orderedMembers)
                    {
                        if (!nodes.TryGetValue(m.NodeI, out var ni) || !nodes.TryGetValue(m.NodeJ, out var nj))
                        {
                            errors++;
                            continue;
                        }

                        XYZ p1 = ToXyz(ni);
                        XYZ p2 = ToXyz(nj);

                        if (p1.IsAlmostEqualTo(p2))
                        {
                            skipped++;
                            continue;
                        }

                        string inputKey = SegmentKey(ElementId.InvalidElementId, p1, p2);
                        if (!seenInputKeys.Add(inputKey))
                        {
                            skipped++;
                            continue;
                        }

                        try
                        {
                            var sym = ResolveFramingSymbol(m.Family, m.Section);
                            Activate(sym);

                            var curve = Line.CreateBound(p1, p2);
                            string modelKey = SegmentKey(sym.Id, p1, p2);
                            if (_existingKeys.Contains(modelKey))
                            {
                                skipped++;
                                continue;
                            }

                            var fi = _doc.Create.NewFamilyInstance(curve, sym, level, StructuralType.Beam);

                            TryDisallowJoin(fi, 0);
                            TryDisallowJoin(fi, 1);
                            ZeroExtensionsIfPresent(fi);

                            newFraming.Add(fi);
                            created++;
                            _existingKeys.Add(modelKey);
                        }
                        catch (Exception ex)
                        {
                            Console.WriteLine("DA: Member create failed, " + ex.Message);
                            errors++;
                        }
                    }

                    _doc.Regenerate();
                    ApplyOrangeOverrideToCategory(_doc);
                    t.Commit();
                }

                return (created, skipped, errors);
            }

            private XYZ ToXyz(Node n)
            {
                return new XYZ(ToFeet(n.X), ToFeet(n.Y), ToFeet(n.Z));
            }

            private double ToFeet(double v)
            {
                if (_units == "m") return v * FeetPerM;
                if (_units == "mm") return v * FeetPerMm;
                if (_units == "ft") return v;
                throw new ArgumentException("Unsupported units, " + _units);
            }

            // Get lowest level or create one near min Z
            private Level GetOrCreateLevel(Dictionary<string, Node> nodes)
            {
                var levels = new FilteredElementCollector(_doc)
                    .OfClass(typeof(Level))
                    .Cast<Level>()
                    .OrderBy(l => l.Elevation)
                    .ToList();

                if (levels.Count > 0)
                    return levels[0];

                if (nodes == null || nodes.Count == 0)
                    throw new InvalidOperationException(
                        "No levels in model and no nodes in input to define a new level.");

                double minNodeZUnits = nodes.Values.Min(n => n.Z);

                double marginUnits;
                double stepUnits;
                switch (_units)
                {
                    case "m":
                        marginUnits = 0.05;
                        stepUnits = 0.10;
                        break;
                    case "mm":
                        marginUnits = 50.0;
                        stepUnits = 100.0;
                        break;
                    case "ft":
                        marginUnits = 0.2;
                        stepUnits = 0.5;
                        break;
                    default:
                        throw new ArgumentException("Unsupported units, " + _units);
                }

                double targetUnits = minNodeZUnits - marginUnits;
                double snappedUnits = Math.Floor(targetUnits / stepUnits) * stepUnits;
                if (Math.Abs(snappedUnits) < stepUnits * 0.5)
                    snappedUnits = 0.0;

                double elevationFt = ToFeet(snappedUnits);

                Console.WriteLine(
                    $"DA: No levels found. Creating level at approx Z={snappedUnits} {_units} ({elevationFt} ft). " +
                    $"Lowest node Z={minNodeZUnits} {_units}.");

                Level newLevel = Level.Create(_doc, elevationFt);
                try
                {
                    newLevel.Name = "DA_Generated_Level";
                }
                catch { }

                HideLevelIn3DViews(newLevel);

                return newLevel;
            }

            private void HideLevelIn3DViews(Level level)
            {
                if (level == null)
                    return;

                var ids = new List<ElementId> { level.Id };

                var views3D = new FilteredElementCollector(_doc)
                    .OfClass(typeof(View3D))
                    .Cast<View3D>()
                    .Where(v => !v.IsTemplate)
                    .ToList();

                foreach (var v in views3D)
                {
                    try
                    {
                        v.HideElements(ids);
                    }
                    catch { }
                }
            }

            private void Activate(FamilySymbol sym)
            {
                if (sym != null && !sym.IsActive)
                {
                    sym.Activate();
                    _doc.Regenerate();
                }
            }

            private static void TryDisallowJoin(FamilyInstance fi, int end)
            {
                try { StructuralFramingUtils.DisallowJoinAtEnd(fi, end); } catch { }
            }

            private static void ZeroExtensionsIfPresent(FamilyInstance fi)
            {
                foreach (Parameter p in fi.Parameters)
                {
                    var def = p.Definition;
                    if (def == null) continue;
                    var name = def.Name;
                    if (string.IsNullOrWhiteSpace(name)) continue;
                    var n = name.ToLowerInvariant();
                    if (!p.IsReadOnly && n.Contains("extension"))
                    {
                        if (n.Contains("start") || n.Contains("inicio") || n.Contains("begin"))
                            try { p.Set(0.0); } catch { }
                        if (n.Contains("end") || n.Contains("final"))
                            try { p.Set(0.0); } catch { }
                    }
                }
            }

            private FamilySymbol ResolveFramingSymbol(string familyName, string typeName)
            {
                string fam = (familyName ?? "").Trim();
                string typ = (typeName ?? "").Trim();

                if (_framingSymbols == null || _framingSymbols.Count == 0)
                    throw new InvalidOperationException("No Structural Framing symbols in the model.");

                var match = TryResolveSymbolInList(_framingSymbols, fam, typ);
                return match ?? _framingSymbols[0];
            }

            private static FamilySymbol TryResolveSymbolInList(List<FamilySymbol> symbols, string familyName, string typeName)
            {
                if (symbols == null || symbols.Count == 0)
                    return null;

                string fam = (familyName ?? "").Trim();
                string typ = (typeName ?? "").Trim();

                if (!string.IsNullOrEmpty(fam) && !string.IsNullOrEmpty(typ))
                {
                    foreach (var s in symbols)
                    {
                        string mf = FamilyNameOf(s);
                        string mt = s.Name;
                        if (string.Equals(mf, fam, StringComparison.Ordinal) &&
                            string.Equals(mt, typ, StringComparison.Ordinal))
                            return s;
                    }
                }

                if (!string.IsNullOrEmpty(typ))
                {
                    foreach (var s in symbols)
                    {
                        if (string.Equals(s.Name, typ, StringComparison.Ordinal))
                            return s;
                    }
                }

                return null;
            }

            private static string FamilyNameOf(FamilySymbol s)
            {
                try { return s.Family?.Name ?? s.FamilyName; }
                catch { return s.Family?.Name; }
            }

            private HashSet<string> CollectExistingFramingSegmentKeys()
            {
                var keys = new HashSet<string>();

                var framing = new FilteredElementCollector(_doc)
                    .OfCategory(BuiltInCategory.OST_StructuralFraming)
                    .OfClass(typeof(FamilyInstance))
                    .Cast<FamilyInstance>();

                foreach (var fi in framing)
                {
                    var lc = fi.Location as LocationCurve;
                    var line = lc?.Curve as Line;
                    if (line == null) continue;

                    XYZ a = line.GetEndPoint(0);
                    XYZ b = line.GetEndPoint(1);
                    var typeId = fi.Symbol?.Id ?? ElementId.InvalidElementId;

                    string key = SegmentKey(typeId, a, b);
                    keys.Add(key);
                }
                return keys;
            }

            private static string SegmentKey(ElementId typeId, XYZ p1, XYZ p2)
            {
                var first = p1;
                var second = p2;
                if (CompareXYZ(p2, p1) < 0)
                {
                    first = p2;
                    second = p1;
                }

                string t = typeId != ElementId.InvalidElementId
                    ? typeId.Value.ToString(CultureInfo.InvariantCulture)
                    : "no_type";

                return $"{t}:{Fmt(first)}|{Fmt(second)}";
            }

            private static int CompareXYZ(XYZ a, XYZ b)
            {
                int cx = RoundRank(a.X).CompareTo(RoundRank(b.X));
                if (cx != 0) return cx;
                int cy = RoundRank(a.Y).CompareTo(RoundRank(b.Y));
                if (cy != 0) return cy;
                return RoundRank(a.Z).CompareTo(RoundRank(b.Z));
            }

            private static string Fmt(XYZ p)
            {
                return $"{R(p.X)},{R(p.Y)},{R(p.Z)}";
            }

            private static double R(double v)
            {
                return Math.Round(v / SegmentTolFt) * SegmentTolFt;
            }

            private static double RoundRank(double v)
            {
                return R(v);
            }

            private static List<FamilySymbol> GetSymbols(Document doc, BuiltInCategory category)
            {
                return new FilteredElementCollector(doc)
                    .OfCategory(category)
                    .OfClass(typeof(FamilySymbol))
                    .Cast<FamilySymbol>()
                    .ToList();
            }

            private static void ApplyOrangeOverrideToCategory(Document doc)
            {
                if (doc == null)
                    return;

                Category framingCat = doc.Settings.Categories.get_Item(BuiltInCategory.OST_StructuralFraming);
                if (framingCat == null)
                    return;

                // Solid fill pattern
                FillPatternElement solidFill = new FilteredElementCollector(doc)
                    .OfClass(typeof(FillPatternElement))
                    .Cast<FillPatternElement>()
                    .FirstOrDefault(fpe => fpe.GetFillPattern().IsSolidFill);

                if (solidFill == null)
                    return;

                // Outline: dark red-brown
                var lineColor = new Color(160, 60, 60);
                // Fill: redder
                var fillColor = new Color(235, 90, 90);

                var ogs = new OverrideGraphicSettings();

                // Lines
                ogs.SetProjectionLineColor(lineColor);
                ogs.SetCutLineColor(lineColor);
                ogs.SetProjectionLineWeight(1);
                ogs.SetCutLineWeight(1);

                // Surface fill
                ogs.SetSurfaceForegroundPatternId(solidFill.Id);
                ogs.SetSurfaceForegroundPatternColor(fillColor);

                // Cut fill
                ogs.SetCutForegroundPatternId(solidFill.Id);
                ogs.SetCutForegroundPatternColor(fillColor);

                var views = new FilteredElementCollector(doc)
                    .OfClass(typeof(View))
                    .Cast<View>()
                    .Where(v => !v.IsTemplate && v.CanCategoryBeHidden(framingCat.Id))
                    .ToList();

                foreach (var v in views)
                {
                    try
                    {
                        v.SetCategoryOverrides(framingCat.Id, ogs);
                    }
                    catch (Autodesk.Revit.Exceptions.InvalidOperationException)
                    {
                        // ignore stubborn views
                    }
                }
            }
        }
    }
}