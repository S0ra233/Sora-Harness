extends SceneTree
## Data-only GDRETools bridge. Game scenes and attached scripts are never run.
## ResourceCompatLoader.fake_load keeps original types and external references.
## Optimized value replacement follows GDRETools v2.6.4's
## compat/optimized_translation_extractor.cpp (MIT), without copying keys
## or message positions from another locale.


func _init():
	GDREConfig.set_setting("execute_visual_shader_node_scripts", false, true)
	var args = OS.get_cmdline_user_args()
	if args.size() != 2:
		push_error("Expected request.json and response.json")
		quit(1)
		return
	var request = JSON.parse_string(FileAccess.get_file_as_string(args[0]))
	if not request is Dictionary or not request.get("jobs") is Array:
		_finish(args[1], {"error": "Invalid bridge request"}, 1)
		return
	var results = []
	for job in request.jobs:
		var result = _dispatch(job)
		result["id"] = job.id
		results.append(result)
	_finish(args[1], {"backend_version": GDRESettings.get_gdre_version(), "results": results}, 0)


func _finish(path: String, value: Dictionary, code: int):
	var file = FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		push_error("Cannot write bridge response: " + path)
		quit(1)
		return
	file.store_string(JSON.stringify(value))
	file.close()
	quit(code)


func _dispatch(job: Dictionary) -> Dictionary:
	match job.operation:
		"to_text":
			var info = ResourceCompatLoader.get_resource_info(job.input)
			var kind = ResourceCompatLoader.get_resource_type(job.input)
			if kind in ["Translation", "TranslationPO", "OptimizedTranslation", "PHashTranslation"]:
				return {"resource_info": info, "resource_type": kind}
			var error = ResourceCompatLoader.to_text(job.input, job.output, 0, job.get("resource_path", ""))
			return {"error": "Resource conversion failed: " + str(error)} if error != OK else {"resource_info": info, "resource_type": kind}
		"to_binary":
			var resource = ResourceCompatLoader.fake_load(job.input)
			if resource == null:
				return {"error": "Cannot load edited resource"}
			return _save(resource, job)
		"translation_read", "translation_write":
			return _translation(job)
	return {"error": "Unknown bridge operation"}


func _save(resource: Resource, job: Dictionary) -> Dictionary:
	var major = int(job.get("major", 0))
	if major not in [3, 4]:
		return {"error": "Resource write requires a confirmed Godot 3/4 version"}
	var error = ResourceCompatLoader.save_custom(resource, job.output, major, int(job.get("minor", 0)), 0)
	return {"error": "Resource save failed: " + str(error)} if error != OK else {"written": true}


func _translation(job: Dictionary) -> Dictionary:
	var resource = ResourceCompatLoader.fake_load(job.input)
	if resource == null:
		return {"error": "Cannot load translation resource"}
	var kind = ResourceCompatLoader.get_resource_type(job.input)
	var records: Array
	if kind in ["OptimizedTranslation", "PHashTranslation"]:
		records = _optimized_records(resource)
	elif kind in ["Translation", "TranslationPO"]:
		var messages = resource.get("messages")
		if not (messages is Dictionary or messages is PackedStringArray):
			return {"error": "Unsupported messages storage for " + kind}
		records = _message_records(messages)
	else:
		return {"error": "Unsupported translation class: " + kind}
	if job.operation == "translation_write":
		var error = _replace_messages(resource, kind, records, job.items)
		if not error.is_empty():
			return {"error": error}
		return _save(resource, job)
	return {"resource_type": kind, "locale": str(resource.get("locale")), "records": records}


func _message_records(messages) -> Array:
	var records = []
	if messages is Dictionary:
		for key in messages:
			if key is Array and key.size() == 2 and messages[key] is Array:
				for form in range(messages[key].size()):
					records.append({"key": str(key[1]), "context": str(key[0]),
						"plural_index": form, "storage_key": key, "text": str(messages[key][form])})
			elif messages[key] is String or messages[key] is StringName:
				records.append({"key": str(key), "storage_key": key, "text": str(messages[key])})
	elif messages is PackedStringArray:
		for index in range(0, messages.size() - 1, 2):
			records.append({"key": messages[index], "text": messages[index + 1], "pair_index": index})
	return records


func _optimized_records(resource: Resource) -> Array:
	# Reading values needs no original keys or hash-version conversion.
	var reader = OptimizedTranslation.new()
	for property in ["hash_table", "bucket_table", "strings"]:
		reader.set(property, resource.get(property))
	var values = reader.get_translated_message_list()
	var table = resource.get("hash_table")
	var buckets = resource.get("bucket_table")
	var records = []
	for offset in table:
		if offset == -1 or offset == 0xffffffff:
			continue
		for index in range(buckets[offset]):
			var slot = int(offset) + 2 + index * 4
			records.append({"index": records.size(), "slot": slot,
				"key_hash": buckets[slot], "text": values[records.size()]})
	return records


func _replace_messages(resource: Resource, kind: String, records: Array, items: Array) -> String:
	var by_key = {}
	for record in records:
		by_key[_record_identity(record)] = record
	for item in items:
		var identity = _record_identity(item)
		if not by_key.has(identity):
			return "Translation location no longer exists: " + identity
		var record = by_key[identity]
		if record.text != item.original:
			return "Translation source changed: " + identity
		if record.has("slot") and (int(record.slot) != int(item.slot) or int(record.key_hash) != int(item.key_hash)):
			return "Translation hash slot changed: " + identity
		if "\u0000" in item.translation:
			return "Translation contains NUL"
		if kind in ["OptimizedTranslation", "PHashTranslation"]:
			_replace_optimized(resource, record, item.translation)
		else:
			var messages = resource.get("messages")
			if messages is Dictionary:
				if record.has("plural_index"):
					messages[record.storage_key][int(record.plural_index)] = item.translation
				else:
					messages[record.storage_key] = item.translation
			else:
				messages[int(record.pair_index) + 1] = item.translation
			resource.set("messages", messages)
	return ""


func _record_identity(record: Dictionary) -> String:
	if record.has("key"):
		return JSON.stringify([str(record.get("context", "")), str(record.key), int(record.get("plural_index", 0))])
	return str(int(record.index))


func _replace_optimized(resource: Resource, record: Dictionary, text: String):
	# Same operation as GDRETools replace_message_in_elem: append an uncompressed
	# UTF-8 value, retain every key hash and all other message locations.
	var buckets = resource.get("bucket_table")
	var strings = resource.get("strings")
	var value = text.to_utf8_buffer()
	value.append(0)
	var slot = int(record.slot)
	buckets[slot + 1] = strings.size()
	buckets[slot + 2] = value.size()
	buckets[slot + 3] = value.size()
	strings.append_array(value)
	resource.set("bucket_table", buckets)
	resource.set("strings", strings)
