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
							:placeholder="__('Opportunity')"
						/>
					</div>
					<div class="min-w-0 grow">
						<FormControl
							v-model="row.name"
							:label="__('Document')"
							:placeholder="__('Document name or ID')"
							@keyup.enter="addRow"
						/>
					</div>
					<div class="min-w-0 grow">
						<FormControl v-model="row.title" :label="__('Title (optional)')" />
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
			</div>
		</template>
	</Dialog>
</template>

<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import { Button, Dialog, FormControl, createResource } from 'frappe-ui'

import { raiseToast } from '@/apps/mail/utils'

const show = defineModel<boolean>()
const { mailName } = defineProps<{ mailName: string }>()

interface DocumentLink {
	doctype: string
	name: string
	title?: string | null
}

const emptyRow = { doctype: '', name: '', title: '' }
const row = reactive({ ...emptyRow })
const links = ref<DocumentLink[]>([])

const addRow = () => {
	if (!(row.doctype && row.name)) return
	if (links.value.some((l) => l.doctype === row.doctype && l.name === row.name)) {
		raiseToast(__('That document is already on the list.'))
		return
	}
	links.value.push({ doctype: row.doctype.trim(), name: row.name.trim(), title: row.title.trim() || null })
	Object.assign(row, emptyRow)
}

const linkDocuments = createResource({
	url: 'suite.mail.api.link.link_to_documents',
	makeParams: () => ({ name: mailName, links: links.value }),
	onSuccess: (data: { links: DocumentLink[] }) => {
		raiseToast(__('Linked to {0} document(s).', [data.links.length]))
		show.value = false
	},
	onError: (error) => raiseToast(error.message, 'error'),
})

watch(show, (val) => {
	if (val) {
		links.value = []
		Object.assign(row, emptyRow)
	}
})
</script>
