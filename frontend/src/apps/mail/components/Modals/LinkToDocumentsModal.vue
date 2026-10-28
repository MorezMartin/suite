<template>
	<Dialog
		v-model:open="show"
		v-bind="{
			title: __('Link to Documents'),
			size: 'md',
			actions: [
				{
					label: __('Link'),
					variant: 'solid',
					disabled: !links.length,
					loading: linkDocuments.loading,
					onClick: linkDocuments.submit,
				},
			],
		}"
	>
		<template #default>
			<div class="space-y-4">
				<div class="text-ink-gray-5 text-p-sm">
					{{
						__(
							'Attach this mail to documents: their timelines show the mail, and later replies stay linked to them.',
						)
					}}
				</div>

				<div class="flex items-end gap-2">
					<div class="min-w-0 grow">
						<FormControl
							v-model="row.doctype"
							:label="__('DocType')"
							:placeholder="__('Sales Invoice')"
						/>
					</div>
					<div class="min-w-0 grow">
						<Combobox
							v-model="row.name"
							v-model:open="showDocSuggestions"
							:label="__('Document')"
							placeholder="INV-2026-00001"
							:options="documentOptions"
							:open-on-click="false"
							@update:query="searchDocuments"
						>
							<template #item-create="{ query }">{{ query }}</template>
						</Combobox>
					</div>
					<Button :label="__('Add')" :disabled="!(row.doctype && row.name)" @click="addRow" />
				</div>

				<div v-if="links.length" class="space-y-2">
					<div
						v-for="(link, index) in links"
						:key="`${link.doctype}/${link.name}`"
						class="bg-surface-gray-2 flex items-center justify-between rounded-4 border px-3 py-2"
					>
						<div class="text-p-sm truncate">
							<span class="text-ink-gray-5">{{ link.doctype }}</span>
							<span class="text-ink-gray-4 mx-1">·</span>
							<span class="text-ink-gray-9">{{ link.title || link.name }}</span>
						</div>
						<Button variant="ghost" :title="__('Remove')" @click="links.splice(index, 1)">
							<template #icon>
								<X class="text-ink-gray-5 icon" />
							</template>
						</Button>
					</div>
				</div>

				<ErrorMessage :message="linkDocumentsError" />
			</div>
		</template>
	</Dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useDebounceFn } from '@vueuse/core'
import { Button, Combobox, Dialog, ErrorMessage, FormControl, createResource } from 'frappe-ui'
import { X } from 'lucide-vue-next'

import { raiseToast } from '@/apps/mail/utils'

const show = defineModel<boolean>()
const { mailName } = defineProps<{ mailName: string }>()

interface DocumentLink {
	doctype: string
	name: string
	title?: string | null
}

const emptyRow = { doctype: '', name: '' }
const row = reactive({ ...emptyRow })
const links = ref<DocumentLink[]>([])

// Document search goes through the desk's own search_link — the same permission checks any Link
// field gets — so suggestions only ever contain documents the user could link to anyway. A typo'd
// doctype just errors the search; the raw typed name still submits and the server re-validates.
const doctypeValid = (doctype: string) => /^[A-Za-z0-9 _-]+$/.test(doctype?.trim() ?? '')
const searchText = ref('')
const showDocSuggestions = ref(false)

const docSearch = createResource({
	url: 'frappe.desk.search.search_link',
	auto: false,
	makeParams: () => ({
		doctype: row.doctype.trim(),
		txt: searchText.value,
		searchfield: 'name',
		page_length: 10,
	}),
	transform: (data: { value: string; description?: string }[]) =>
		data.map((o) => ({
			value: o.value,
			label: o.description ? `${o.value} (${o.description})` : o.value,
		})),
})

const fetchDocs = useDebounceFn(() => {
	if (doctypeValid(row.doctype) && searchText.value) docSearch.fetch()
}, 300)

const searchDocuments = (text: string) => {
	searchText.value = text
	if (!text) showDocSuggestions.value = false
	fetchDocs()
}

// Suggestions only exist for a typed query — with an empty input the popover would show stale
// results from the previous query (or a bare "No results" panel), so block reka's focus opens too.
watch(showDocSuggestions, (open) => {
	if (open && !searchText.value) showDocSuggestions.value = false
})

// Search hits plus a "use what you typed" row for exact names the search cannot see (case
// differences, codes); the server validates the target either way.
const documentOptions = computed(() => {
	if (!searchText.value) return []
	return [
		...(docSearch.data ?? []),
		{
			type: 'custom',
			slot: 'create',
			condition: () => !docSearch.data?.length,
			onClick: ({ query }: { query: string }) => {
				row.name = query
			},
		},
	]
})

const addRow = () => {
	if (!(row.doctype && row.name)) return
	const doctype = row.doctype.trim()
	const name = row.name.trim()
	if (links.value.some((l) => l.doctype === doctype && l.name === name)) {
		raiseToast(__('That document is already on the list.'))
		return
	}
	links.value.push({ doctype, name })
	Object.assign(row, emptyRow)
	docSearch.reset()
}

const linkDocuments = createResource({
	url: 'suite.mail.api.link.link_to_documents',
	makeParams: () => ({ name: mailName, links: links.value }),
	onSuccess: (data: { links: DocumentLink[] }) => {
		raiseToast(__('Linked to {0} document(s).', [data.links.length]))
		show.value = false
	},
})

const linkDocumentsError = computed(() => {
	const error = linkDocuments.error
	if (!error) return ''
	return error.messages?.[0] || error.message || ''
})

watch(show, (val) => {
	if (val) {
		links.value = []
		Object.assign(row, emptyRow)
		linkDocuments.reset()
		docSearch.reset()
	}
})
</script>
