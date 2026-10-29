<template>
	<Teleport to="body">
		<Transition
			enter-active-class="transition duration-200 ease-out"
			enter-from-class="translate-x-full"
			enter-to-class="translate-x-0"
			leave-active-class="transition duration-150 ease-in"
			leave-from-class="translate-x-0"
			leave-to-class="translate-x-full"
		>
			<div
				v-if="open"
				class="bg-surface-modal fixed inset-y-0 right-0 z-50 flex w-full max-w-sm flex-col border-l shadow-2xl"
			>
				<div class="flex items-center justify-between border-b px-4 py-3">
					<h3 class="text-base-semibold">{{ __('Linked Documents') }}</h3>
					<Button variant="ghost" @click="emit('update:open', false)">
						<template #icon><X class="text-ink-gray-5 h-4 w-4" /></template>
					</Button>
				</div>

				<div class="flex-1 overflow-y-auto p-3">
					<div v-if="documents.loading && !documents.data?.length" class="space-y-2">
						<div
							v-for="i in 2"
							:key="i"
							class="border-surface-gray-3 h-20 animate-pulse rounded-6 border"
						></div>
					</div>

					<div
						v-else-if="!documents.data?.length"
						class="text-ink-gray-5 pt-10 text-center text-sm"
					>
						{{ __('No linked documents yet.') }}
					</div>

					<div v-else class="space-y-2">
						<div
							v-for="doc in documents.data"
							:key="`${doc.doctype}/${doc.name}`"
							class="border-surface-gray-3 bg-surface-group-2 rounded-6 border p-3"
						>
							<div class="flex items-start justify-between gap-2">
								<a class="min-w-0 flex-1" :href="deskRoute(doc)" target="_blank">
									<div class="text-ink-gray-5 flex items-center gap-1.5 text-xs">
										<Badge :label="doc.doctype_label" variant="subtle" />
										<Badge
											v-if="doc.source === 'auto'"
											:label="__('auto')"
											variant="light-gray"
										/>
									</div>
									<div class="text-ink-gray-9 mt-1.5 truncate text-sm font-medium">
										{{ doc.title }}
									</div>
									<div class="text-ink-gray-5 mt-0.5 truncate font-mono text-xs">
										{{ doc.name }}
									</div>
									<div v-if="doc.status" class="mt-1.5 flex items-center gap-1 text-xs">
										<span class="bg-blue-500 h-2 w-2 shrink-0 rounded-full"></span>
										<span class="text-ink-gray-6">{{ doc.status }}</span>
									</div>
									<div v-if="doc.preview?.length" class="mt-1.5 space-y-0.5">
										<div
											v-for="p in doc.preview"
											:key="p.label"
											class="text-ink-gray-5 flex gap-1.5 text-xs"
										>
											<span class="shrink-0">{{ p.label }}:</span>
											<span class="text-ink-gray-7 truncate">{{ p.value }}</span>
										</div>
									</div>
								</a>
								<Button
									variant="ghost"
									:tooltip="__('Unlink')"
									:disabled="unlink.loading"
									@click="unlink.submit(doc)"
								>
									<template #icon><X class="text-ink-gray-5 h-4 w-4" /></template>
								</Button>
							</div>
						</div>
					</div>
				</div>
			</div>
		</Transition>
	</Teleport>
</template>

<script setup lang="ts">
import { watch } from 'vue'
import { X } from 'lucide-vue-next'
import { Badge, Button, createResource } from 'frappe-ui'

import { raiseToast } from '@/apps/mail/utils'
import type { Mail } from '@/apps/mail/types'

interface LinkedDocument {
	doctype: string
	name: string
	title: string
	doctype_label: string
	source: 'auto' | 'manual'
	status?: string
	preview?: { label: string; value: string }[]
}

const { open, mail } = defineProps<{ open: boolean; mail: Mail }>()
const emit = defineEmits<{ 'update:open': [boolean] }>()

const documents = createResource({
	url: 'suite.mail.api.link.get_message_linked_documents',
	makeParams: () => ({ name: mail.name }),
})

const unlink = createResource({
	url: 'suite.mail.api.link.unlink_document',
	makeParams: (doc: LinkedDocument) => ({
		name: mail.name,
		doctype: doc.doctype,
		docname: doc.name,
	}),
	onSuccess: () => {
		raiseToast(__('Document unlinked.'))
		documents.fetch()
	},
	onError: (error) => raiseToast(error.message, 'error'),
})

function deskRoute(doc: LinkedDocument) {
	const slug = doc.doctype.toLowerCase().replace(/ /g, '-')
	return `/app/${slug}/${encodeURIComponent(doc.name)}`
}

// The panel refetches each time it opens — link states change between threads, and it is
// one merged query scoped to a single Communication.
watch(
	() => [open, mail.name] as const,
	([isOpen]) => isOpen && documents.fetch(),
)
</script>
