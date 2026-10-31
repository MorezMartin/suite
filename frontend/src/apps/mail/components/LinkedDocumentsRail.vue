<template>
	<!-- The linked-documents rail of the reading pane — a tile, not an overlay: it sits in the
	     pane's own flex row (MailThread), so the toolbar and every other control stay usable
	     beside it. Scope: the open conversation (every message of the thread), with rows born on
	     the focused message highlighted; the linking itself stays per-message. Attachments of
	     the conversation show below as display-only rows. -->
	<div
		class="border-surface-gray-3 bg-surface-base flex h-full w-72 shrink-0 flex-col overflow-hidden border-l xl:w-80"
	>
		<div class="border-b px-3 py-2">
			<div class="flex items-center justify-between">
				<h3 class="text-ink-gray-8 text-xs font-semibold uppercase tracking-wide">
					{{ __('Linked Documents') }}
				</h3>
				<Button variant="ghost" class="!h-6 !w-6" :tooltip="__('Close')" @click="emit('close')">
					<template #icon><X class="text-ink-gray-5 h-3.5 w-3.5" /></template>
				</Button>
			</div>
			<div class="mt-2 flex items-center gap-1.5">
				<div class="relative min-w-0 flex-1">
					<Search class="text-ink-gray-4 pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
					<input
						v-model="search"
						class="border-surface-gray-3 bg-surface-gray-1 text-ink-gray-8 placeholder:text-ink-gray-4 w-full rounded-md border py-1 pl-7 pr-2 text-xs outline-none focus:border-gray-400"
						:placeholder="__('Filter…')"
					/>
				</div>
				<Button v-if="!readonly" :label="__('Link')" variant="solid" size="xs" @click="emit('link')" />
			</div>
			<div class="text-ink-gray-6 mt-1.5 flex items-center gap-1.5 text-xs">
				<Dropdown :options="doctypeFilterItems" align="start">
					<Button variant="subtle" size="xs" class="max-w-32 truncate">
						{{ doctypeFilter || __('Doctype') }}
						<ChevronDown class="ml-1 h-3 w-3 shrink-0" />
					</Button>
				</Dropdown>
				<Dropdown :options="sortItems" align="start">
					<Button variant="subtle" size="xs">
						{{ sortLabel }}
						<ChevronDown class="ml-1 h-3 w-3 shrink-0" />
					</Button>
				</Dropdown>
				<Button
					variant="ghost"
					size="xs"
					class="ml-auto !h-6"
					:tooltip="__('Refresh')"
					@click="documents.reload()"
				>
					<template #icon><RefreshCw class="h-3 w-3" /></template>
				</Button>
			</div>
		</div>

		<div class="flex-1 overflow-y-auto p-2">
			<div v-if="documents.loading && !documents.data?.length" class="space-y-2">
				<div
					v-for="i in 3"
					:key="i"
					class="border-surface-gray-3 h-16 animate-pulse rounded-6 border"
				></div>
			</div>

			<div
				v-else-if="!shown.length && !attachmentRows.length"
				class="text-ink-gray-4 flex h-24 flex-col items-center justify-center gap-1 text-center text-xs"
			>
				<component :is="documents.data?.length ? SearchX : Paperclip" class="h-4 w-4" />
				<span>{{
					documents.data?.length
						? __('No document matches the filters.')
						: __('No linked document yet.')
				}}</span>
			</div>

			<div v-if="shown.length" class="space-y-1.5">
				<div
					v-for="doc in shown"
					:key="`${doc.doctype}/${doc.name}`"
					class="border-surface-gray-3 bg-surface-group-2 group rounded-6 border p-2"
					:class="{ 'ring-blue-500 ring-1': isCurrentMailLink(doc) }"
				>
					<div class="flex items-start gap-1">
						<a class="min-w-0 flex-1" :href="deskRoute(doc)" target="_blank">
							<div class="text-ink-gray-5 flex flex-wrap items-center gap-1 text-[10px]">
								<Badge :label="doc.doctype_label" variant="subtle" />
								<Badge
									v-if="doc.source === 'auto'"
									:label="__('auto')"
									variant="light-gray"
								/>
								<span
									v-if="doc.mail && doc.mail !== mail?.name"
									class="text-ink-gray-4 max-w-32 truncate"
								>
									· {{ __('on another message') }}
								</span>
							</div>
							<div class="text-ink-gray-9 mt-1 truncate text-xs font-medium">
								{{ doc.title }}
							</div>
							<div v-if="doc.status" class="mt-0.5 flex items-center gap-1 text-[10px]">
								<span class="bg-blue-500 h-1.5 w-1.5 shrink-0 rounded-full"></span>
								<span class="text-ink-gray-5">{{ doc.status }}</span>
							</div>
							<div v-if="doc.preview?.length" class="mt-0.5 space-y-px">
								<div
									v-for="p in doc.preview"
									:key="p.label"
									class="text-ink-gray-5 flex gap-1 text-[10px]"
								>
									<span class="shrink-0">{{ p.label }}:</span>
									<span class="text-ink-gray-6 truncate">{{ p.value }}</span>
								</div>
							</div>
						</a>
						<Button
							v-if="!readonly"
							variant="ghost"
							size="xs"
							class="!h-6 !w-6 opacity-0 transition-opacity group-hover:opacity-100"
							:tooltip="__('Unlink')"
							:disabled="unlink.loading"
							@click="askUnlink(doc)"
						>
							<template #icon><Unlink class="text-ink-gray-5 h-3.5 w-3.5" /></template>
						</Button>
					</div>
				</div>
			</div>

			<!-- Attachments of the conversation: display-only rows (the blobs are JMAP files,
			     not documents to link). Opening hands the blob back to the pane's viewer. -->
			<div v-if="attachmentRows.length" class="mt-3 border-t pt-2">
				<div class="text-ink-gray-4 mb-1.5 text-[10px] font-semibold uppercase tracking-wide">
					{{ __('Attachments ({0})', [String(attachmentRows.length)]) }}
				</div>
				<div class="space-y-1">
					<button
						v-for="(att, idx) in attachmentRows"
						:key="`${att.mail_name}/${att.filename}/${idx}`"
						class="hover:bg-surface-group-2 flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left"
						@click="emit('open-attachment', att)"
					>
						<File class="text-ink-gray-4 h-3.5 w-3.5 shrink-0" />
						<span class="text-ink-gray-7 min-w-0 flex-1 truncate text-xs">{{ att.filename }}</span>
						<span class="text-ink-gray-4 shrink-0 text-[10px]">{{ humanSize(att.size) }}</span>
					</button>
				</div>
			</div>
		</div>

		<!-- Unlink is deliberate: one confirmation per row, worded for the scope (a discussion
		     linked as a whole unlinks every message of the thread). -->
		<Dialog
			v-model:open="showUnlinkDialog"
			:options="{
				title: __('Unlink {0}?', [unlinkTarget?.doctype_label || '']),
				body: unlinkConfirmBody,
				actions: [
					{
						label: __('Unlink'),
						variant: 'solid',
						loading: unlink.loading,
						onClick: () => unlink.submit(unlinkTarget.value),
					},
				],
			}"
		/>
	</div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
	ChevronDown,
	File,
	Paperclip,
	RefreshCw,
	Search,
	SearchX,
	Unlink,
	X,
} from 'lucide-vue-next'
import { Badge, Button, Dialog, Dropdown, createResource } from 'frappe-ui'

import { raiseToast } from '@/apps/mail/utils'
import type { Attachment, Mail } from '@/apps/mail/types'

interface LinkedDocument {
	doctype: string
	name: string
	title: string
	doctype_label: string
	source: 'auto' | 'manual'
	creation?: string | null
	mail?: string
	status?: string
	preview?: { label: string; value: string }[]
}

const { mail = null, thread = null, account = '', readonly = false } = defineProps<{
	/** The message the reader focuses on (highlight anchor inside the thread scope). */
	mail?: Mail | null
	/** All messages of the open conversation — the panel's scope. */
	thread?: Mail[] | null
	/** JMAP account id the thread lives on (All Inboxes may show another account's thread). */
	account?: string
	readonly?: boolean
}>()
const emit = defineEmits<{
	close: []
	link: []
	'open-attachment': [attachment: Attachment & { mail_name: string }]
}>()

const threadId = computed(() => thread?.[0]?.thread_id ?? mail?.thread_id)
const scopeNames = computed(() => (thread?.length ? thread.map((m) => m.name) : [mail?.name]))

const search = ref('')
const doctypeFilter = ref('')
const sortBy = ref<'creation' | 'doctype'>('creation')
const sortAsc = ref(false)

const documents = createResource({
	url: 'suite.mail.api.link.get_thread_linked_documents',
	// No client cache: the rail must reflect fresh link state each time it opens or re-scopes —
	// unlinking in one pane has to be visible in the other.
	makeParams: () => ({ account, thread_id: threadId.value }),
	onError: (error) => raiseToast(error.message, 'error'),
})

// Fetch on mount and whenever the rail is re-scoped to another conversation while staying open.
watch(
	() => [account, threadId.value] as const,
	() => account && threadId.value && documents.fetch(),
	{ immediate: true },
)

const sortLabel = computed(() => {
	if (sortBy.value === 'doctype') return sortAsc.value ? __('Doctype A–Z') : __('Doctype Z–A')
	return sortAsc.value ? __('Oldest first') : __('Newest first')
})

const sortItems = computed(() => [
	{ label: __('Newest first'), onClick: () => ((sortBy.value = 'creation'), (sortAsc.value = false)) },
	{ label: __('Oldest first'), onClick: () => ((sortBy.value = 'creation'), (sortAsc.value = true)) },
	{ label: __('Doctype A–Z'), onClick: () => ((sortBy.value = 'doctype'), (sortAsc.value = true)) },
	{ label: __('Doctype Z–A'), onClick: () => ((sortBy.value = 'doctype'), (sortAsc.value = false)) },
])

// Doctype filter: the distinct doctypes currently linked (classic list filter, scoped payload).
const doctypeFilterItems = computed(() => {
	const counts = new Map<string, number>()
	for (const d of documents.data ?? []) counts.set(d.doctype, (counts.get(d.doctype) ?? 0) + 1)
	return [
		{ label: __('All doctypes'), onClick: () => (doctypeFilter.value = '') },
		...[...counts.entries()].map(([doctype, n]) => ({
			label: `${doctype} (${n})`,
			onClick: () => (doctypeFilter.value === doctype ? '' : (doctypeFilter.value = doctype)),
		})),
	]
})

const shown = computed(() => {
	let rows: LinkedDocument[] = [...(documents.data ?? [])]
	if (search.value.trim()) {
		const q = search.value.trim().toLowerCase()
		rows = rows.filter(
			(d) =>
				d.title?.toLowerCase().includes(q) ||
				d.name?.toLowerCase().includes(q) ||
				d.doctype?.toLowerCase().includes(q),
		)
	}
	if (doctypeFilter.value) {
		rows = rows.filter((d) => d.doctype === doctypeFilter.value)
	}
	const factor = sortAsc.value ? 1 : -1
	rows.sort((a, b) => {
		if (sortBy.value === 'doctype') return factor * a.doctype.localeCompare(b.doctype)
		return factor * (a.creation || '').localeCompare(b.creation || '')
	})
	return rows
})

function isCurrentMailLink(doc: LinkedDocument) {
	return !!doc.mail && doc.mail === mail?.name
}

const attachmentRows = computed(() => {
	const mails = thread?.length ? thread : mail ? [mail] : []
	const rows: (Attachment & { mail_name: string })[] = []
	for (const m of mails) {
		for (const a of m.attachments ?? []) {
			if (a.disposition !== 'attachment') continue
			rows.push({ ...a, mail_name: m.name })
		}
	}
	if (search.value.trim()) {
		const q = search.value.trim().toLowerCase()
		return rows.filter((a) => a.filename?.toLowerCase().includes(q))
	}
	return rows
})

function humanSize(size: string | number) {
	const bytes = Number(size)
	if (!bytes) return ''
	if (bytes < 1024) return `${bytes} B`
	if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
	return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function deskRoute(doc: LinkedDocument) {
	const slug = doc.doctype.toLowerCase().replace(/ /g, '-')
	return `/app/${slug}/${encodeURIComponent(doc.name)}`
}

const showUnlinkDialog = ref(false)
const unlinkTarget = ref<LinkedDocument | null>(null)

const unlinkConfirmBody = computed(() => {
	if (!unlinkTarget.value) return ''
	return __('Remove the link between this document and {0}?', [
		thread?.length ? __('every message of this conversation') : __('this message'),
	])
})

function askUnlink(doc: LinkedDocument) {
	unlinkTarget.value = doc
	showUnlinkDialog.value = true
}

const unlink = createResource({
	url: 'suite.mail.api.link.unlink_document',
	makeParams: (doc: LinkedDocument) => ({
		name: doc.mail || mail?.name,
		doctype: doc.doctype,
		docname: doc.name,
		// Thread scope: the link is lifted from every message of the discussion.
		names: thread?.length ? JSON.stringify(scopeNames.value) : undefined,
	}),
	onSuccess: (data: { removed: number; links: LinkedDocument[] }) => {
		raiseToast(__('Document unlinked from {0} message(s).', [String(data.removed)]))
		showUnlinkDialog.value = false
		documents.reload()
	},
	onError: (error) => raiseToast(error.message, 'error'),
})
</script>
